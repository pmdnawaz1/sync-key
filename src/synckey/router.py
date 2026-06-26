"""Routing: model name -> ordered provider list, with tier-floored fallback.

Resolution order:
  1. Explicit provider/model prefix (groq/llama-3.3-70b-versatile)
  2. Live model index (what your keys actually advertise, ordered by priority)
  3. Static name patterns (gemini-2.0-flash -> gemini)

Tier floor: when all providers for the requested model are exhausted, we look
for same-tier alternatives in the index (never dropping below the floor).
Opus -> GPT-5 is fine. Opus -> gpt-oss is not.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from .config import Settings
from .db import Database
from .providers import Provider, match_by_pattern, split_provider_prefix
from .tiers import TIER_NAMES, model_tier


class Resolution:
    __slots__ = ("bare_model", "providers", "how", "tier", "floor")

    def __init__(
        self,
        bare_model: str,
        providers: list[str],
        how: str,
        tier: int | None = None,
        floor: int | None = None,
    ):
        self.bare_model = bare_model
        self.providers = providers
        self.how = how
        self.tier = tier      # detected tier of the requested model
        self.floor = floor    # fallback is not allowed below this tier


def order_by_priority(providers: list[str], priority: list[str]) -> list[str]:
    rank = {pid: i for i, pid in enumerate(priority)}
    return sorted(providers, key=lambda p: rank.get(p, len(priority)))


def parse_model_ids(payload: dict) -> list[str]:
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("data"), list):
        return [
            str(item.get("id") or item.get("name"))
            for item in payload["data"]
            if isinstance(item, dict) and (item.get("id") or item.get("name"))
        ]
    if isinstance(payload.get("models"), list):
        return [
            str(m.get("id") or m.get("name"))
            for m in payload["models"]
            if isinstance(m, dict) and (m.get("id") or m.get("name"))
        ]
    return []


class Router:
    def __init__(self, db: Database, providers: dict[str, Provider], settings: Settings):
        self.db = db
        self.providers = providers
        self.settings = settings
        self._known_ids = set(providers)
        self._index: dict[str, set[str]] = {}
        self._tier_index: dict[int, list[str]] = {}  # tier -> [model_ids in index]
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
            self._rebuild_tier_index()
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

    def _rebuild_tier_index(self) -> None:
        self._tier_index = {}
        for model in self._index:
            t = model_tier(model)
            if t is not None:
                self._tier_index.setdefault(t, []).append(model)

    @property
    def index(self) -> dict[str, set[str]]:
        return self._index

    def index_age(self) -> float:
        return time.time() - self._index_built_at if self._index_built_at else float("inf")

    async def refresh_index(self, secret_for) -> dict[str, list[str]]:
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
        self._rebuild_tier_index()
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

    def resolve(self, model: str, floor: int | None = None) -> Resolution:
        pid, bare = split_provider_prefix(model, self._known_ids)
        tier = model_tier(bare if pid else model)
        effective_floor = floor if floor is not None else tier

        if pid:
            return Resolution(bare, [pid], "prefix", tier=tier, floor=effective_floor)

        providers = self._index.get(model)
        if providers:
            ordered = order_by_priority(sorted(providers), self.settings.provider_priority)
            return Resolution(model, ordered, "index", tier=tier, floor=effective_floor)

        guess = match_by_pattern(model, self.providers)
        if guess:
            return Resolution(model, [guess], "pattern", tier=tier, floor=effective_floor)

        return Resolution(model, [], "unknown", tier=tier, floor=effective_floor)

    def tier_alternatives(self, tier: int, floor: int, exclude_model: str) -> list[tuple[str, str]]:
        """Find (model, provider) pairs in the index at the given tier (and >= floor).

        Used when all providers for the primary model are exhausted and we want
        to fall back to an equivalent-quality alternative.
        """
        out: list[tuple[str, str]] = []
        for t in sorted(self._tier_index, reverse=True):
            if t < floor or t != tier:
                continue
            for m in self._tier_index.get(t, []):
                if m == exclude_model:
                    continue
                for pid in order_by_priority(
                    sorted(self._index.get(m, set())), self.settings.provider_priority
                ):
                    out.append((m, pid))
        return out
