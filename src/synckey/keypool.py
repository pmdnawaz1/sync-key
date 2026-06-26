"""The key pool: round-robin selection with rate-limit eating.

Goals:
* Spread load across every enabled key for a provider (round-robin, weighted).
* When a key gets 429'd or errors, put it on a cooldown (honoring
  ``Retry-After``) and *immediately* hand the caller the next live key.
* Persist cooldown / health to SQLite so restarts and the CLI agree on state.

The pool is provider-scoped: callers ask for an ordered list of candidate keys
for a provider and try them in order until one succeeds.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass

from .crypto import SecretBox
from .db import Database, KeyRecord


@dataclass
class Candidate:
    key_id: int
    label: str
    secret: str
    cooling: bool
    cooldown_remaining: float


class KeyPool:
    def __init__(self, db: Database, box: SecretBox, default_cooldown: float = 20.0):
        self.db = db
        self.box = box
        self.default_cooldown = default_cooldown
        # Per-provider monotonically increasing rotation cursor.
        self._cursors: dict[str, itertools.count] = {}

    def _cursor(self, provider: str) -> int:
        if provider not in self._cursors:
            self._cursors[provider] = itertools.count()
        return next(self._cursors[provider])

    def candidates(self, provider: str) -> list[Candidate]:
        """Return live-first, round-robin-ordered candidates for a provider.

        Available (non-cooling) keys come first, rotated so consecutive calls
        start at a different key.  Keys still in cooldown are appended last,
        ordered by who frees up soonest — a usable fallback if everything is
        currently throttled.
        """
        now = time.time()
        records = self.db.list_keys(provider=provider, enabled_only=True)
        if not records:
            return []

        # Weighted expansion: a key with weight N appears N times so it gets a
        # proportionally larger share of the rotation.
        expanded: list[KeyRecord] = []
        for r in records:
            expanded.extend([r] * max(1, r.weight))

        start = self._cursor(provider) % len(expanded)
        rotated = expanded[start:] + expanded[:start]

        available: list[Candidate] = []
        cooling: list[Candidate] = []
        seen: set[int] = set()
        for r in rotated:
            if r.id in seen:
                continue
            seen.add(r.id)
            state = self.db.get_state(r.id)
            cooldown_until = state["cooldown_until"] if state else 0.0
            remaining = max(0.0, cooldown_until - now)
            cand = Candidate(
                key_id=r.id,
                label=r.label,
                secret=self.box.open(r.secret),
                cooling=remaining > 0,
                cooldown_remaining=remaining,
            )
            (cooling if cand.cooling else available).append(cand)

        cooling.sort(key=lambda c: c.cooldown_remaining)
        return available + cooling

    # --- outcome reporting ---------------------------------------------------
    def report_success(self, key_id: int) -> None:
        self.db.mark_success(key_id)

    def report_rate_limit(self, key_id: int, retry_after: float | None = None) -> None:
        cooldown = retry_after if retry_after and retry_after > 0 else self.default_cooldown
        self.db.mark_cooldown(key_id, time.time() + cooldown)

    def report_failure(self, key_id: int, backoff: float | None = None) -> None:
        """Transient server error: short, escalating cooldown."""
        state = self.db.get_state(key_id)
        fails = (state["consecutive_failures"] if state else 0) + 1
        delay = backoff if backoff is not None else min(self.default_cooldown, 2.0 * fails)
        self.db.mark_cooldown(key_id, time.time() + delay)


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (seconds form only; HTTP-date is rare here)."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
