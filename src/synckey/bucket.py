"""Per-key proactive rate buckets.

The goal: never send a request that we predict will 429.

Each key gets a token bucket sized to its observed RPM (and optionally TPM).
Before picking a key, we check can_send() -- if it returns False, the key is
proactively skipped. This turns the reactive cycle of "send -> get 429 -> wait"
into "check bucket -> skip -> try next key" with zero wasted upstream calls.

Limit discovery is adaptive:
  - Keys start with unlimited capacity (no limit known yet).
  - On first 429, the current emission rate is recorded as the ceiling and the
    bucket is tightened below it.
  - Retry-After tells us how long the provider is cooling us, which implies the
    reset window; we use that to infer a safer RPM.
  - After N consecutive successes the cap is relaxed by 10% (up to the observed
    max).

Users can also declare RPM/TPM per key in config to skip the discovery phase.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Bucket:
    # Requests per minute limit. None = no limit known yet.
    rpm_cap: float | None = None
    # Tokens per minute limit. None = no limit known yet.
    tpm_cap: float | None = None
    # Current available request tokens (refills continuously).
    _req_tokens: float = field(default=0.0, init=False, repr=False)
    # Current available token-count tokens.
    _tok_tokens: float = field(default=0.0, init=False, repr=False)
    _last_refill: float = field(default_factory=time.monotonic, init=False, repr=False)
    # Rolling request count in the last 60s for emission-rate tracking.
    _window: list[float] = field(default_factory=list, init=False, repr=False)
    _consecutive_ok: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._req_tokens = float(self.rpm_cap or 1_000_000)
        self._tok_tokens = float(self.tpm_cap or 1_000_000_000)
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._last_refill = now
        if self.rpm_cap is not None:
            self._req_tokens = min(self.rpm_cap, self._req_tokens + elapsed * self.rpm_cap / 60.0)
        if self.tpm_cap is not None:
            self._tok_tokens = min(self.tpm_cap, self._tok_tokens + elapsed * self.tpm_cap / 60.0)

    def emission_rpm(self) -> float:
        """Estimate actual current emission rate (requests per minute)."""
        now = time.time()
        cutoff = now - 60.0
        self._window = [t for t in self._window if t > cutoff]
        return len(self._window) * (60.0 / max(1.0, now - (self._window[0] if self._window else now)))

    def can_send(self, estimated_tokens: int = 0) -> bool:
        self._refill()
        if self.rpm_cap is not None and self._req_tokens < 1.0:
            return False
        if self.tpm_cap is not None and self._tok_tokens < estimated_tokens:
            return False
        return True

    def consume(self, estimated_tokens: int = 0) -> None:
        self._window.append(time.time())
        if len(self._window) > 500:
            self._window = self._window[-500:]
        self._req_tokens = max(0.0, self._req_tokens - 1.0)
        if self.tpm_cap is not None and estimated_tokens:
            self._tok_tokens = max(0.0, self._tok_tokens - estimated_tokens)

    def on_success(self) -> None:
        self._consecutive_ok += 1
        # After 20 consecutive successes, relax the cap by 10%.
        if self._consecutive_ok >= 20 and self.rpm_cap is not None:
            self.rpm_cap = min(self.rpm_cap * 1.1, self.rpm_cap * 2)
            self._consecutive_ok = 0

    def on_rate_limit(self, retry_after: float | None, rpm_observed: float | None = None) -> None:
        """Tighten the cap based on a 429 response."""
        self._consecutive_ok = 0
        if rpm_observed and rpm_observed > 0:
            # Set cap to 80% of observed emission rate at time of 429.
            new_cap = rpm_observed * 0.80
            self.rpm_cap = new_cap if self.rpm_cap is None else min(self.rpm_cap, new_cap)
        elif self.rpm_cap is None:
            # We had no cap; seed from Retry-After window.
            if retry_after and retry_after > 0:
                # If the window is e.g. 20s, the provider is doing 60/20 = 3 rpm.
                # Be conservative and set it lower.
                self.rpm_cap = max(1.0, (60.0 / retry_after) * 0.7)
            else:
                self.rpm_cap = 30.0  # conservative default
        else:
            self.rpm_cap = max(1.0, self.rpm_cap * 0.75)
        # Drain the bucket so we don't immediately send more.
        self._req_tokens = 0.0

    def headroom(self) -> float:
        """Fraction of capacity still available (1.0 = full, 0.0 = empty)."""
        self._refill()
        if self.rpm_cap is None:
            return 1.0
        return self._req_tokens / self.rpm_cap if self.rpm_cap > 0 else 0.0


class BucketRegistry:
    """Creates and caches one Bucket per key_id."""

    def __init__(self) -> None:
        self._buckets: dict[int, Bucket] = {}

    def get(self, key_id: int, rpm_cap: float | None = None, tpm_cap: float | None = None) -> Bucket:
        if key_id not in self._buckets:
            self._buckets[key_id] = Bucket(rpm_cap=rpm_cap, tpm_cap=tpm_cap)
        b = self._buckets[key_id]
        # If the caller now knows the caps (e.g. loaded from config), apply them.
        if rpm_cap is not None and b.rpm_cap is None:
            b.rpm_cap = rpm_cap
            b._req_tokens = rpm_cap
        if tpm_cap is not None and b.tpm_cap is None:
            b.tpm_cap = tpm_cap
            b._tok_tokens = tpm_cap
        return b

    def drop(self, key_id: int) -> None:
        self._buckets.pop(key_id, None)
