"""SQLite persistence: credentials, usage records, runtime key state, metadata.

A thin synchronous wrapper is fine here — SQLite writes are fast and the
gateway records usage off the hot path.  WAL mode keeps the CLI readable while
the server writes.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    provider   TEXT NOT NULL,
    label      TEXT NOT NULL,
    secret     BLOB NOT NULL,           -- Fernet-sealed
    enabled    INTEGER NOT NULL DEFAULT 1,
    weight     INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS key_state (
    key_id               INTEGER PRIMARY KEY REFERENCES keys(id) ON DELETE CASCADE,
    cooldown_until       REAL NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    total_requests       INTEGER NOT NULL DEFAULT 0,
    total_failures       INTEGER NOT NULL DEFAULT 0,
    last_used            REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    key_id            INTEGER,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    status_code       INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    stream            INTEGER NOT NULL DEFAULT 0,
    error             TEXT
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage(provider);
CREATE INDEX IF NOT EXISTS idx_keys_provider ON keys(provider);
"""


@dataclass
class KeyRecord:
    id: int
    provider: str
    label: str
    secret: bytes
    enabled: bool
    weight: int
    created_at: float


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- meta ----------------------------------------------------------------
    def set_meta(self, name: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(name, value) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (name, value),
        )
        self.conn.commit()

    def get_meta(self, name: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE name=?", (name,)).fetchone()
        return row["value"] if row else None

    # --- keys ----------------------------------------------------------------
    def add_key(self, provider: str, label: str, secret: bytes, weight: int = 1) -> int:
        cur = self.conn.execute(
            "INSERT INTO keys(provider, label, secret, weight, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (provider, label, secret, weight, time.time()),
        )
        key_id = int(cur.lastrowid)
        self.conn.execute("INSERT INTO key_state(key_id) VALUES(?)", (key_id,))
        self.conn.commit()
        return key_id

    def remove_key(self, key_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM keys WHERE id=?", (key_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def set_key_enabled(self, key_id: int, enabled: bool) -> None:
        self.conn.execute("UPDATE keys SET enabled=? WHERE id=?", (1 if enabled else 0, key_id))
        self.conn.commit()

    def list_keys(self, provider: str | None = None, enabled_only: bool = False) -> list[KeyRecord]:
        sql = "SELECT * FROM keys"
        clauses, params = [], []
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        if enabled_only:
            clauses.append("enabled=1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY provider, id"
        rows = self.conn.execute(sql, params).fetchall()
        return [
            KeyRecord(
                id=r["id"],
                provider=r["provider"],
                label=r["label"],
                secret=r["secret"],
                enabled=bool(r["enabled"]),
                weight=r["weight"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def providers_with_keys(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT provider FROM keys WHERE enabled=1 ORDER BY provider"
        ).fetchall()
        return [r["provider"] for r in rows]

    # --- key state -----------------------------------------------------------
    def get_state(self, key_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM key_state WHERE key_id=?", (key_id,)).fetchone()

    def mark_cooldown(self, key_id: int, until: float) -> None:
        self.conn.execute(
            "UPDATE key_state SET cooldown_until=?, consecutive_failures=consecutive_failures+1, "
            "total_failures=total_failures+1 WHERE key_id=?",
            (until, key_id),
        )
        self.conn.commit()

    def mark_success(self, key_id: int) -> None:
        self.conn.execute(
            "UPDATE key_state SET consecutive_failures=0, last_used=?, "
            "total_requests=total_requests+1 WHERE key_id=?",
            (time.time(), key_id),
        )
        self.conn.commit()

    def mark_attempt(self, key_id: int) -> None:
        self.conn.execute(
            "UPDATE key_state SET last_used=?, total_requests=total_requests+1 WHERE key_id=?",
            (time.time(), key_id),
        )
        self.conn.commit()

    # --- usage ---------------------------------------------------------------
    def record_usage(
        self,
        *,
        provider: str,
        model: str,
        key_id: int | None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        status_code: int = 0,
        latency_ms: int = 0,
        stream: bool = False,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO usage(ts, provider, model, key_id, prompt_tokens, completion_tokens, "
            "total_tokens, status_code, latency_ms, stream, error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                provider,
                model,
                key_id,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                status_code,
                latency_ms,
                1 if stream else 0,
                error,
            ),
        )
        self.conn.commit()

    def usage_summary(self, since: float | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT provider, model, COUNT(*) AS requests, "
            "SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, "
            "SUM(total_tokens) AS total_tokens, "
            "SUM(CASE WHEN status_code>=400 OR error IS NOT NULL THEN 1 ELSE 0 END) AS errors, "
            "AVG(latency_ms) AS avg_latency "
            "FROM usage"
        )
        params: list = []
        if since is not None:
            sql += " WHERE ts>=?"
            params.append(since)
        sql += " GROUP BY provider, model ORDER BY total_tokens DESC"
        return self.conn.execute(sql, params).fetchall()

    def usage_totals(self, since: float | None = None) -> sqlite3.Row:
        sql = (
            "SELECT COUNT(*) AS requests, SUM(total_tokens) AS total_tokens, "
            "SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, "
            "SUM(CASE WHEN status_code>=400 OR error IS NOT NULL THEN 1 ELSE 0 END) AS errors "
            "FROM usage"
        )
        params: list = []
        if since is not None:
            sql += " WHERE ts>=?"
            params.append(since)
        return self.conn.execute(sql, params).fetchone()

    def recent_usage(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM usage ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
