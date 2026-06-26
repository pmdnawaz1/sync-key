"""Batched off-thread usage writer.

The gateway calls record() once per request. Non-blocking: the row queues and
a background thread flushes in batches. One fsync per batch instead of per request.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from pathlib import Path

from .db import USAGE_INSERT, tune

Row = tuple


class UsageWriter:
    def __init__(self, path: Path, batch_size: int = 200, flush_interval: float = 0.5):
        self.path = Path(path)
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.queue: queue.Queue[Row] = queue.Queue(maxsize=20000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="synckey-usage", daemon=True)
        self._thread.start()

    def record(
        self,
        *,
        provider: str,
        model: str,
        key_id: int | None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float | None = None,
        status_code: int = 0,
        latency_ms: int = 0,
        stream: bool = False,
        tier: int | None = None,
        error: str | None = None,
    ) -> None:
        row = (
            time.time(),
            provider,
            model,
            key_id,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cost_usd,
            status_code,
            latency_ms,
            1 if stream else 0,
            tier,
            error,
        )
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            pass  # drop metrics under extreme backpressure

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        tune(conn)
        try:
            while True:
                batch = self._collect()
                if batch:
                    conn.executemany(USAGE_INSERT, batch)
                    conn.commit()
                elif self._stop.is_set() and self.queue.empty():
                    return
        finally:
            conn.close()

    def _collect(self) -> list[Row]:
        batch: list[Row] = []
        try:
            batch.append(self.queue.get(timeout=self.flush_interval))
        except queue.Empty:
            return batch
        while len(batch) < self.batch_size:
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return batch
