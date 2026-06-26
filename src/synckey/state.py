"""Durable key health state.

In-memory dict is the hot path (zero I/O on reads). SQLite is the durability
layer: written immediately on every health transition so a restart never forgets
which keys are cooling or dead.

Health transitions:
    LIVE    -> COOLING  on 429 (auto-recovers after cooldown_until)
    LIVE    -> DEAD     on 401/403 or explicit quota exhaustion
    COOLING -> LIVE     automatically when cooldown_until passes (no write needed)
    DEAD    -> LIVE     only via `synckey key enable` (user action)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum

import sqlite3
from pathlib import Path


class Health(IntEnum):
    LIVE    = 0
    COOLING = 1
    DEAD    = 2


@dataclass
class KeyState:
    health: Health = Health.LIVE
    cooldown_until: float = 0.0
    dead_reason: str = ""
    rpm_observed: float | None = None
    tpm_observed: float | None = None

    def is_available(self) -> bool:
        if self.health == Health.DEAD:
            return False
        if self.health == Health.COOLING and time.time() < self.cooldown_until:
            return False
        return True

    def cooldown_remaining(self) -> float:
        if self.health == Health.COOLING:
            return max(0.0, self.cooldown_until - time.time())
        return 0.0


_CREATE = """
CREATE TABLE IF NOT EXISTS key_state (
    key_id          INTEGER PRIMARY KEY,
    health          INTEGER NOT NULL DEFAULT 0,
    cooldown_until  REAL    NOT NULL DEFAULT 0,
    dead_reason     TEXT    NOT NULL DEFAULT '',
    rpm_observed    REAL,
    tpm_observed    REAL,
    updated_at      REAL    NOT NULL DEFAULT 0
);
"""

_UPSERT = """
INSERT INTO key_state(key_id, health, cooldown_until, dead_reason, rpm_observed, tpm_observed, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(key_id) DO UPDATE SET
    health         = excluded.health,
    cooldown_until = excluded.cooldown_until,
    dead_reason    = excluded.dead_reason,
    rpm_observed   = excluded.rpm_observed,
    tpm_observed   = excluded.tpm_observed,
    updated_at     = excluded.updated_at
"""


class StateStore:
    """In-memory key health store backed by SQLite for durability."""

    def __init__(self, db_path: Path):
        self._mem: dict[int, KeyState] = {}
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_CREATE)
        self._conn.commit()
        self._load()

    def _load(self) -> None:
        now = time.time()
        for row in self._conn.execute("SELECT * FROM key_state").fetchall():
            key_id, health, cooldown_until, dead_reason, rpm_obs, tpm_obs, _ = row
            h = Health(health)
            # Expired cooldowns auto-recover to LIVE on load.
            if h == Health.COOLING and cooldown_until <= now:
                h = Health.LIVE
                cooldown_until = 0.0
            self._mem[key_id] = KeyState(
                health=h,
                cooldown_until=cooldown_until,
                dead_reason=dead_reason or "",
                rpm_observed=rpm_obs,
                tpm_observed=tpm_obs,
            )

    def _persist(self, key_id: int) -> None:
        st = self._mem[key_id]
        self._conn.execute(
            _UPSERT,
            (
                key_id,
                int(st.health),
                st.cooldown_until,
                st.dead_reason,
                st.rpm_observed,
                st.tpm_observed,
                time.time(),
            ),
        )
        self._conn.commit()

    def get(self, key_id: int) -> KeyState:
        return self._mem.get(key_id, KeyState())

    def set_cooling(self, key_id: int, until: float, rpm_observed: float | None = None) -> None:
        st = self._mem.get(key_id, KeyState())
        st.health = Health.COOLING
        st.cooldown_until = until
        if rpm_observed is not None:
            st.rpm_observed = rpm_observed
        self._mem[key_id] = st
        self._persist(key_id)

    def set_dead(self, key_id: int, reason: str) -> None:
        st = self._mem.get(key_id, KeyState())
        st.health = Health.DEAD
        st.dead_reason = reason
        self._mem[key_id] = st
        self._persist(key_id)

    def set_live(self, key_id: int) -> None:
        st = self._mem.get(key_id, KeyState())
        if st.health != Health.LIVE:
            st.health = Health.LIVE
            st.cooldown_until = 0.0
            self._mem[key_id] = st
            self._persist(key_id)

    def all(self) -> dict[int, KeyState]:
        return dict(self._mem)

    def close(self) -> None:
        self._conn.close()
