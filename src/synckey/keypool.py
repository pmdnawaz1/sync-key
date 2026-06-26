"""Key pool: proactive + tier-aware selection.

Selection order for a provider:
  1. LIVE keys whose bucket has capacity  (best: no 429 risk)
  2. LIVE keys whose bucket is exhausted  (might 429 but key itself is healthy)
  3. COOLING keys, soonest-free first     (last resort)
  DEAD keys are never returned.

This means the gateway stops sending to a key *before* it 429s, not after.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .bucket import Bucket, BucketRegistry
from .crypto import SecretBox
from .db import Database, KeyRecord
from .state import Health, StateStore


@dataclass
class Candidate:
    key_id: int
    label: str
    secret: str
    provider: str
    ready: bool          # True if bucket has capacity right now
    cooling: bool
    cooldown_remaining: float
    bucket: Bucket


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class KeyPool:
    def __init__(
        self,
        db: Database,
        box: SecretBox,
        states: StateStore,
        default_cooldown: float = 20.0,
    ):
        self.db = db
        self.box = box
        self.states = states
        self.default_cooldown = default_cooldown
        self._secrets: dict[int, str] = {}
        self._buckets = BucketRegistry()
        self._cursor: dict[str, int] = {}

    def _secret(self, rec: KeyRecord) -> str:
        s = self._secrets.get(rec.id)
        if s is None:
            s = self.box.open(rec.secret)
            self._secrets[rec.id] = s
        return s

    def candidates(self, provider: str, estimated_tokens: int = 0) -> list[Candidate]:
        records = self.db.list_keys(provider=provider, enabled_only=True)
        if not records:
            return []

        weighted: list[KeyRecord] = []
        for r in records:
            weighted.extend([r] * max(1, r.weight))

        start = self._cursor.get(provider, 0) % len(weighted)
        self._cursor[provider] = start + 1
        rotated = weighted[start:] + weighted[:start]

        now = time.time()
        ready: list[Candidate] = []
        throttled: list[Candidate] = []
        cooling: list[Candidate] = []
        seen: set[int] = set()

        for r in rotated:
            if r.id in seen:
                continue
            seen.add(r.id)

            st = self.states.get(r.id)
            if st.health == Health.DEAD:
                continue

            bucket = self._buckets.get(r.id, rpm_cap=r.rpm_limit, tpm_cap=r.tpm_limit)
            remaining = max(0.0, st.cooldown_until - now) if st.health == Health.COOLING else 0.0
            is_cooling = remaining > 0
            can_send = bucket.can_send(estimated_tokens)

            cand = Candidate(
                key_id=r.id,
                label=r.label,
                secret=self._secret(r),
                provider=provider,
                ready=can_send and not is_cooling,
                cooling=is_cooling,
                cooldown_remaining=remaining,
                bucket=bucket,
            )
            if is_cooling:
                cooling.append(cand)
            elif can_send:
                ready.append(cand)
            else:
                throttled.append(cand)

        cooling.sort(key=lambda c: c.cooldown_remaining)
        return ready + throttled + cooling

    def on_success(self, key_id: int) -> None:
        self.states.set_live(key_id)
        self._buckets.get(key_id).on_success()

    def on_rate_limit(self, key_id: int, retry_after: float | None, rpm_at_429: float | None = None) -> None:
        cooldown = retry_after if retry_after and retry_after > 0 else self.default_cooldown
        self.states.set_cooling(key_id, time.time() + cooldown, rpm_observed=rpm_at_429)
        self._buckets.get(key_id).on_rate_limit(retry_after, rpm_at_429)

    def on_dead(self, key_id: int, reason: str) -> None:
        self.states.set_dead(key_id, reason)

    def on_failure(self, key_id: int) -> None:
        st = self.states.get(key_id)
        fails = (1 if st.health == Health.LIVE else 2)
        delay = min(self.default_cooldown, 2.0 * fails)
        self.states.set_cooling(key_id, time.time() + delay)

    def consume(self, key_id: int, estimated_tokens: int = 0) -> None:
        self._buckets.get(key_id).consume(estimated_tokens)
