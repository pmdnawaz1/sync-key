"""Routing: turn an incoming model name into an ordered list of providers.

Resolution order (first hit wins, but yields an *ordered* candidate list so the
gateway can fail over):

1. Explicit ``provider/model`` prefix when the prefix is a known provider id.
2. Live model index — providers whose ``/models`` actually advertise the model,
   ordered by configured priority.  This is authoritative: it reflects what your
   keys can really call right now.
3. Static name patterns (``gemini-*`` -> gemini, ``command-*`` -> cohere, ...).

The model index is also what powers ``synckey models``.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from .config import Settings
from .db import Database
from .providers import Provider, detect_by_pattern, explicit_prefix


class Resolution:
    def __init__(self, bare_model: str, providers: list[str], how: str):
        self.bare_model = bare_model
        self.providers = providers
        self.how = how  # "prefix" | "index" | "pattern" | "unknown"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Resolution(model={self.bare_model!r}, providers={self.providers}, how={self.how})"


def _ordered_by_priority(providers: list[str], priority: list[str]) -> list[str]:
    rank = {pid: i for i, pid in enumerate(priority)}
    return sorted(providers, key=lambda p: rank.get(p, len(priority)))


class Router:
    def __init__(
        self,
        db: Database,
        providers: dict[str, Provider],
        settings: Settings,
    ):
        self.db = db
        self.providers = providers
        self.settings = settings
        # model_id -> set of provider ids
        self._index: dict[str, set[str]] = {}
        self._index_built_at: float = 0.0
        self._load_cached_index()

    # --- index ---------------------------------------------------------------
    def _load_cached_index(self) -> None:
        raw = self.db.get_meta("model_index")
        if raw:
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

    async def refresh_index(self, secret_for: "callable") -> dict[str, list[str]]:
        """Rebuild the model index by querying each configured provider's /models.

        ``secret_for(provider_id) -> str | None`` supplies a usable credential.
        Returns a per-provider {provider_id: [errors|count]} report-ish map of
        model lists actually discovered.
        """
        configured = self.db.providers_with_keys()
        new_index: dict[str, set[str]] = {}
        discovered: dict[str, list[str]] = {}

        async with httpx.AsyncClient(timeout=30.0) as client:
            results = await asyncio.gather(
                *(self._fetch_models(client, pid, secret_for(pid)) for pid in configured),
                return_exceptions=True,
            )

        for pid, result in zip(configured, results):
            if isinstance(result, Exception):
                discovered[pid] = []
                continue
            models = result
            discovered[pid] = models
            for m in models:
                new_index.setdefault(m, set()).add(pid)

        self._index = new_index
        self._index_built_at = time.time()
        self._save_cached_index()
        return discovered

    async def _fetch_models(self, client: httpx.AsyncClient, pid: str, secret: str | None) -> list[str]:
        prov = self.providers[pid]
        if not secret and prov.auth != "none":
            return []
        url = prov.base_url.rstrip("/") + prov.models_path
        headers = prov.auth_headers(secret or "")
        params = prov.auth_params(secret or "")
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        payload = resp.json()
        return _extract_model_ids(payload)

    # --- resolution ----------------------------------------------------------
    def resolve(self, model: str) -> Resolution:
        known = set(self.providers.keys())
        pid, bare = explicit_prefix(model, known)
        if pid:
            return Resolution(bare, [pid], "prefix")

        # Live index (authoritative).
        if model in self._index:
            ordered = _ordered_by_priority(
                sorted(self._index[model]), self.settings.provider_priority
            )
            return Resolution(model, ordered, "index")

        # Static patterns.
        guess = detect_by_pattern(model, self.providers)
        if guess:
            return Resolution(model, [guess], "pattern")

        return Resolution(model, [], "unknown")


def _extract_model_ids(payload: dict) -> list[str]:
    """Normalize the various /models response shapes into a flat id list."""
    if not isinstance(payload, dict):
        return []
    # OpenAI shape: {"data": [{"id": ...}]}
    if isinstance(payload.get("data"), list):
        out = []
        for item in payload["data"]:
            if isinstance(item, dict):
                mid = item.get("id") or item.get("name")
                if mid:
                    out.append(str(mid))
        return out
    # GitHub Models catalog shape: [{"id": ...}, ...]
    if isinstance(payload.get("models"), list):
        return [str(m.get("id") or m.get("name")) for m in payload["models"] if isinstance(m, dict)]
    return []
