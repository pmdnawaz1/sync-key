"""SQLite persistence: credentials, usage records, events, metadata."""

from __future__ import annotations

import hashlib
import json
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
    provider   TEXT    NOT NULL,
    label      TEXT    NOT NULL,
    secret     BLOB    NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    weight     INTEGER NOT NULL DEFAULT 1,
    rpm_limit  REAL,
    tpm_limit  REAL,
    created_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL    NOT NULL,
    provider          TEXT    NOT NULL,
    model             TEXT    NOT NULL,
    key_id            INTEGER,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL,
    status_code       INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    stream            INTEGER NOT NULL DEFAULT 0,
    tier              INTEGER,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    type        TEXT    NOT NULL,
    key_id      INTEGER,
    provider    TEXT,
    model       TEXT,
    fallback_to TEXT,
    tier_from   INTEGER,
    tier_to     INTEGER,
    chain       TEXT,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS deferred (
    id            TEXT    PRIMARY KEY,
    created_at    REAL    NOT NULL,
    status        TEXT    NOT NULL,        -- queued | running | done | error
    path          TEXT    NOT NULL,        -- e.g. /chat/completions
    body          TEXT    NOT NULL,        -- JSON request body (non-stream)
    model         TEXT,                    -- original model string (may be null)
    provider_hint TEXT,
    floor         INTEGER,
    eta           REAL,                    -- approx unix time the call can run
    response      BLOB,                    -- stored upstream bytes when done
    status_code   INTEGER,
    error         TEXT,
    completed_at  REAL,
    attempts      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage(provider);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_keys_provider ON keys(provider);
CREATE INDEX IF NOT EXISTS idx_deferred_status ON deferred(status);

CREATE TABLE IF NOT EXISTS routing_chains (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    model      TEXT    NOT NULL,
    provider   TEXT    NOT NULL,
    key_id     INTEGER,
    chain      TEXT    NOT NULL
);
"""

USAGE_COLS = (
    "ts, provider, model, key_id, prompt_tokens, completion_tokens, "
    "total_tokens, cost_usd, status_code, latency_ms, stream, tier, error"
)
USAGE_INSERT = f"INSERT INTO usage({USAGE_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"


def tune(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-8000")


@dataclass
class KeyRecord:
    id: int
    provider: str
    label: str
    secret: bytes
    enabled: bool
    weight: int
    rpm_limit: float | None
    tpm_limit: float | None
    created_at: float


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        tune(self.conn)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # meta
    def set_meta(self, name: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(name, value) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (name, value),
        )
        self.conn.commit()

    def get_meta(self, name: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE name=?", (name,)).fetchone()
        return row["value"] if row else None

    def get_json(self, name: str, default=None):
        raw = self.get_meta(name)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_json(self, name: str, obj) -> None:
        self.set_meta(name, json.dumps(obj))

    # keys
    def add_key(
        self,
        provider: str,
        label: str,
        secret: bytes,
        weight: int = 1,
        rpm_limit: float | None = None,
        tpm_limit: float | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO keys(provider, label, secret, weight, rpm_limit, tpm_limit, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (provider, label, secret, weight, rpm_limit, tpm_limit, time.time()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def remove_key(self, key_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM keys WHERE id=?", (key_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def set_key_enabled(self, key_id: int, enabled: bool) -> None:
        self.conn.execute("UPDATE keys SET enabled=? WHERE id=?", (1 if enabled else 0, key_id))
        self.conn.commit()

    def set_key_limits(self, key_id: int, rpm: float | None, tpm: float | None) -> None:
        self.conn.execute(
            "UPDATE keys SET rpm_limit=?, tpm_limit=? WHERE id=?", (rpm, tpm, key_id)
        )
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
        return [
            KeyRecord(
                id=r["id"],
                provider=r["provider"],
                label=r["label"],
                secret=r["secret"],
                enabled=bool(r["enabled"]),
                weight=r["weight"],
                rpm_limit=r["rpm_limit"],
                tpm_limit=r["tpm_limit"],
                created_at=r["created_at"],
            )
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def providers_with_keys(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT provider FROM keys WHERE enabled=1 ORDER BY provider"
        ).fetchall()
        return [r["provider"] for r in rows]

    def key_stats(self) -> dict[int, dict]:
        """Per-key request/error/token/cost totals from usage log."""
        rows = self.conn.execute(
            "SELECT key_id, COUNT(*) reqs, "
            "SUM(CASE WHEN status_code>=400 OR error IS NOT NULL THEN 1 ELSE 0 END) errs, "
            "SUM(total_tokens) tokens, SUM(cost_usd) cost "
            "FROM usage WHERE key_id IS NOT NULL GROUP BY key_id"
        ).fetchall()
        return {
            r["key_id"]: {
                "requests": r["reqs"],
                "errors": r["errs"] or 0,
                "tokens": r["tokens"] or 0,
                "cost": r["cost"] or 0.0,
            }
            for r in rows
        }

    # events
    def record_event(
        self,
        *,
        type: str,
        key_id: int | None = None,
        provider: str | None = None,
        model: str | None = None,
        fallback_to: str | None = None,
        tier_from: int | None = None,
        tier_to: int | None = None,
        chain: str | None = None,
        message: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events(ts,type,key_id,provider,model,fallback_to,tier_from,tier_to,chain,message) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (time.time(), type, key_id, provider, model, fallback_to, tier_from, tier_to, chain, message),
        )
        self.conn.commit()

    def record_routing_chain(
        self,
        *,
        model: str,
        provider: str,
        key_id: int,
        chain: str,
    ) -> None:
        """Record the full routing chain for a request for TUI view 4."""
        self.conn.execute(
            "INSERT INTO routing_chains(ts, model, provider, key_id, chain) VALUES(?,?,?,?,?)",
            (time.time(), model, provider, key_id, chain),
        )
        self.conn.commit()

    def recent_events(self, limit: int = 50, event_type: str | None = None) -> list[sqlite3.Row]:
        if event_type:
            return self.conn.execute(
                "SELECT * FROM events WHERE type=? ORDER BY ts DESC LIMIT ?", (event_type, limit)
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    def recent_routing_chains(self, limit: int = 20) -> list[sqlite3.Row]:
        """Return the most recent routing chains for TUI view 4."""
        return self.conn.execute(
            "SELECT * FROM routing_chains ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    def routing_chain_by_id(self, chain_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM routing_chains WHERE id=?", (chain_id,)
        ).fetchone()

    # usage reads
    def usage_summary(self, since: float | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT provider, model, tier, COUNT(*) requests, "
            "SUM(prompt_tokens) prompt_tokens, SUM(completion_tokens) completion_tokens, "
            "SUM(total_tokens) total_tokens, SUM(cost_usd) cost_usd, "
            "SUM(CASE WHEN status_code>=400 OR error IS NOT NULL THEN 1 ELSE 0 END) errors, "
            "AVG(latency_ms) avg_latency FROM usage"
        )
        params: list = []
        if since is not None:
            sql += " WHERE ts>=?"
            params.append(since)
        sql += " GROUP BY provider, model ORDER BY total_tokens DESC"
        return self.conn.execute(sql, params).fetchall()

    def usage_totals(self, since: float | None = None) -> sqlite3.Row:
        sql = (
            "SELECT COUNT(*) requests, SUM(total_tokens) total_tokens, "
            "SUM(prompt_tokens) prompt_tokens, SUM(completion_tokens) completion_tokens, "
            "SUM(cost_usd) cost_usd, "
            "SUM(CASE WHEN status_code>=400 OR error IS NOT NULL THEN 1 ELSE 0 END) errors "
            "FROM usage"
        )
        params: list = []
        if since is not None:
            sql += " WHERE ts>=?"
            params.append(since)
        return self.conn.execute(sql, params).fetchone()

    def recent_usage(self, limit: int = 25) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM usage ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    # deferred queue
    def create_deferred(
        self,
        *,
        id: str,
        path: str,
        body: str,
        model: str | None,
        provider_hint: str | None,
        floor: int | None,
        eta: float | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO deferred(id, created_at, status, path, body, model, "
            "provider_hint, floor, eta) VALUES(?,?,?,?,?,?,?,?,?)",
            (id, time.time(), "queued", path, body, model, provider_hint, floor, eta),
        )
        self.conn.commit()

    def get_deferred(self, id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM deferred WHERE id=?", (id,)).fetchone()

    def queued_deferred(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM deferred WHERE status='queued' ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()

    def count_deferred(self, status: str | None = None) -> int:
        if status:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM deferred WHERE status=?", (status,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) c FROM deferred").fetchone()
        return row["c"]

    def recent_deferred(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM deferred ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def set_deferred_running(self, id: str) -> None:
        self.conn.execute(
            "UPDATE deferred SET status='running', attempts=attempts+1 WHERE id=?", (id,)
        )
        self.conn.commit()

    def requeue_deferred(self, id: str, eta: float | None) -> None:
        self.conn.execute(
            "UPDATE deferred SET status='queued', eta=? WHERE id=?", (eta, id)
        )
        self.conn.commit()

    def complete_deferred(self, id: str, status_code: int, response: bytes) -> None:
        self.conn.execute(
            "UPDATE deferred SET status='done', status_code=?, response=?, completed_at=? "
            "WHERE id=?",
            (status_code, response, time.time(), id),
        )
        self.conn.commit()

    def fail_deferred(self, id: str, status_code: int, error: str) -> None:
        self.conn.execute(
            "UPDATE deferred SET status='error', status_code=?, error=?, completed_at=? "
            "WHERE id=?",
            (status_code, error[:500], time.time(), id),
        )
        self.conn.commit()

    def reset_running_deferred(self) -> int:
        """On startup, return any 'running' jobs (from a crash) to the queue."""
        cur = self.conn.execute(
            "UPDATE deferred SET status='queued' WHERE status='running'"
        )
        self.conn.commit()
        return cur.rowcount

    def purge_deferred(self, ttl: float, max_queue_age: float) -> int:
        """Delete finished jobs older than `ttl` and queued jobs stuck past `max_queue_age`."""
        now = time.time()
        cur = self.conn.execute(
            "DELETE FROM deferred WHERE "
            "(status IN ('done','error') AND completed_at IS NOT NULL AND completed_at < ?) "
            "OR (status='queued' AND created_at < ?)",
            (now - ttl, now - max_queue_age),
        )
        self.conn.commit()
        return cur.rowcount
