"""Routing: turn a model name into an ordered list of providers to try.

Order: explicit provider/model prefix, then the live model index (what your
keys actually advertise, ordered by configured priority), then static name
patterns. The index also powers `synckey models`.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from .config import Settings
from .db import Database
from .providers import Provider, match_by_pattern, split_provider_prefix


class Resolution:
    __slots__ = ("bare_model", "providers", "how")

    def __init__(self, bare_model: str, providers: list[str], how: str):
        self.bare_model = bare_model
        self.providers = providers
        self.how = how  # "prefix" | "index" | "pattern" | "unknown"


def order_by_priority(providers: list[str], priority: list[str]) -> list[str]:
    rank = {pid: i for i, pid in enumerate(priority)}
    return sorted(providers, key=lambda p: rank.get(p, len(priority)))


def parse_model_ids(payload: dict) -> list[str]:
    """Normalize the different /models response shapes into a flat id list."""
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("data"), list):
        out = []
        for item in payload["data"]:
            if isinstance(item, dict):
                mid = item.get("id") or item.get("name")
                if mid:
                    out.append(str(mid))
        return out
    if isinstance(payload.get("models"), list):
        return [str(m.get("id") or m.get("name")) for m in payload["models"] if isinstance(m, dict)]
    return []


class Router:
    def __init__(self, db: Database, providers: dict[str, Provider], settings: Settings):
        self.db = db
        self.providers = providers
        self.settings = settings
        self._known_ids = set(providers)
        self._index: dict[str, set[str]] = {}
        self._index_built_at: float = 0.0
        self._load_cached_index()

    def _load_cached_index(self) -> None:
        raw = self.db.get_meta("model_index")
        if not raw:
            return
        try:
            data = json.loads(raw)
            self._index = {m: set(ps) for m, ps in data.get("index", {}).items()}
            self._index_built_at = data.get("built_at", 0.0)
        except (ValueError, KeyError):
            self._index = {}

    def _save_cached_index(self) -> None:
        self.db.set_meta(
            "model_index",
            json.dumps(
                {
                    "built_at": self._index_built_at,
                    "index": {m: sorted(ps) for m, ps in self._index.items()},
                }
            ),
        )

    @property
    def index(self) -> dict[str, set[str]]:
        return self._index

    def index_age(self) -> float:
        return time.time() - self._index_built_at if self._index_built_at else float("inf")

    async def refresh_index(self, secret_for) -> dict[str, list[str]]:
        """Rebuild the index by querying each configured provider's /models.

        secret_for(provider_id) -> str | None supplies a usable credential.
        Returns the models discovered per provider.
        """
        configured = self.db.providers_with_keys()
        index: dict[str, set[str]] = {}
        discovered: dict[str, list[str]] = {}

        async with httpx.AsyncClient(timeout=30.0) as client:
            results = await asyncio.gather(
                *(self._fetch_models(client, pid, secret_for(pid)) for pid in configured),
                return_exceptions=True,
            )

        for pid, result in zip(configured, results):
            models = [] if isinstance(result, Exception) else result
            discovered[pid] = models
            for m in models:
                index.setdefault(m, set()).add(pid)

        self._index = index
        self._index_built_at = time.time()
        self._save_cached_index()
        return discovered

    async def _fetch_models(self, client: httpx.AsyncClient, pid: str, secret: str | None) -> list[str]:
        prov = self.providers[pid]
        if not secret and prov.auth != "none":
            return []
        url = prov.base_url.rstrip("/") + prov.models_path
        resp = await client.get(
            url, headers=prov.auth_headers(secret or ""), params=prov.auth_params(secret or "")
        )
        resp.raise_for_status()
        return parse_model_ids(resp.json())

    def resolve(self, model: str) -> Resolution:
        pid, bare = split_provider_prefix(model, self._known_ids)
        if pid:
            return Resolution(bare, [pid], "prefix")

        providers = self._index.get(model)
        if providers:
            ordered = order_by_priority(sorted(providers), self.settings.provider_priority)
            return Resolution(model, ordered, "index")

        guess = match_by_pattern(model, self.providers)
        if guess:
            return Resolution(model, [guess], "pattern")

        return Resolution(model, [], "unknown")
