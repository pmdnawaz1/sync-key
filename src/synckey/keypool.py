"""Key pool: round-robin selection with rate-limit eating.

Runtime state (cooldowns, failure counts, rotation cursor) lives in memory, and
decrypted secrets are cached, so picking a key never hits the database or runs a
Fernet decrypt on the hot path. The key roster is read from SQLite per call,
which is a cheap WAL read and keeps the pool in sync when the CLI changes keys.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .crypto import SecretBox
from .db import Database, KeyRecord


@dataclass
class Candidate:
    key_id: int
    label: str
    secret: str
    cooling: bool
    cooldown_remaining: float


@dataclass
class KeyState:
    cooldown_until: float = 0.0
    consecutive_failures: int = 0


class KeyPool:
    def __init__(self, db: Database, box: SecretBox, default_cooldown: float = 20.0):
        self.db = db
        self.box = box
        self.default_cooldown = default_cooldown
        self._state: dict[int, KeyState] = {}
        self._secrets: dict[int, str] = {}
        self._cursor: dict[str, int] = {}

    def _secret_for(self, rec: KeyRecord) -> str:
        cached = self._secrets.get(rec.id)
        if cached is None:
            cached = self.box.open(rec.secret)
            self._secrets[rec.id] = cached
        return cached

    def _state_for(self, key_id: int) -> KeyState:
        st = self._state.get(key_id)
        if st is None:
            st = KeyState()
            self._state[key_id] = st
        return st

    def candidates(self, provider: str) -> list[Candidate]:
        """Live keys first (round-robin), then cooling keys soonest-free first."""
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
        available: list[Candidate] = []
        cooling: list[Candidate] = []
        seen: set[int] = set()
        for r in rotated:
            if r.id in seen:
                continue
            seen.add(r.id)
            remaining = max(0.0, self._state_for(r.id).cooldown_until - now)
            cand = Candidate(
                key_id=r.id,
                label=r.label,
                secret=self._secret_for(r),
                cooling=remaining > 0,
                cooldown_remaining=remaining,
            )
            (cooling if cand.cooling else available).append(cand)

        cooling.sort(key=lambda c: c.cooldown_remaining)
        return available + cooling

    def report_success(self, key_id: int) -> None:
        self._state_for(key_id).consecutive_failures = 0

    def report_rate_limit(self, key_id: int, retry_after: float | None = None) -> None:
        st = self._state_for(key_id)
        cooldown = retry_after if retry_after and retry_after > 0 else self.default_cooldown
        st.cooldown_until = time.time() + cooldown
        st.consecutive_failures += 1

    def report_failure(self, key_id: int, backoff: float | None = None) -> None:
        st = self._state_for(key_id)
        st.consecutive_failures += 1
        delay = backoff if backoff is not None else min(self.default_cooldown, 2.0 * st.consecutive_failures)
        st.cooldown_until = time.time() + delay


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
