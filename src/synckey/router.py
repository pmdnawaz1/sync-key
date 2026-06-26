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
from .prefs import Prefs
from .providers import Provider, match_by_pattern, split_provider_prefix
from .tiers import TIER_BY_NAME, model_tier


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
    def __init__(
        self,
        db: Database,
        providers: dict[str, Provider],
        settings: Settings,
        prefs: Prefs | None = None,
    ):
        self.db = db
        self.providers = providers
        self.settings = settings
        self.prefs = prefs or Prefs(db)
        self._known_ids = set(providers)
        self._index: dict[str, set[str]] = {}
        self._tier_index: dict[int, list[str]] = {}  # tier -> [model_ids in index]
        self._index_built_at: float = 0.0
        # User defaults/aliases are read once at startup (hot-path: no per-request
        # DB read). CLI changes apply on the next `serve`; call reload_prefs() to
        # pick them up in-process (tests, the dashboard).
        self._aliases: dict[str, str] = {}
        self._global_default: str | None = None
        self._provider_defaults: dict[str, str] = {}
        self.reload_prefs()
        self._load_cached_index()

    def reload_prefs(self) -> None:
        self._aliases = {k.lower(): v for k, v in self.prefs.aliases().items()}
        self._global_default = self.prefs.global_default() or None
        self._provider_defaults = {
            k.lower(): v for k, v in self.prefs.provider_defaults().items()
        }

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
        tier = model_tier(model)
        effective_floor = floor if floor is not None else tier

        # Index-first: if the full model string is in the live index, use it.
        # This prevents org-namespaced names like "openai/gpt-oss-120b" from
        # being mis-routed by prefix splitting (e.g. to OpenAI when it lives
        # on Groq).
        providers = self._index.get(model)
        if providers:
            ordered = order_by_priority(sorted(providers), self.settings.provider_priority)
            return Resolution(model, ordered, "index", tier=tier, floor=effective_floor)

        # Explicit prefix (groq/llama-3.3-70b-versatile).
        pid, bare = split_provider_prefix(model, self._known_ids)
        if pid:
            tier = model_tier(bare)
            effective_floor = floor if floor is not None else tier
            return Resolution(bare, [pid], "prefix", tier=tier, floor=effective_floor)

        # Static name patterns.
        guess = match_by_pattern(model, self.providers)
        if guess:
            return Resolution(model, [guess], "pattern", tier=tier, floor=effective_floor)

        return Resolution(model, [], "unknown", tier=tier, floor=effective_floor)

    def _models_at_tier(self, tier: int) -> list[str]:
        """Models in the live index at `tier`, ordered by their best provider's priority."""
        prio = self.settings.provider_priority

        def best_rank(model: str) -> int:
            provs = self._index.get(model, set())
            if not provs:
                return len(prio) + 1
            return min(prio.index(p) if p in prio else len(prio) for p in provs)

        return sorted(self._tier_index.get(tier, []), key=best_rank)

    def materialize(self, value: str) -> str | None:
        """Expand an alias or tier name into a concrete model id.

        - `value` may be an alias ("fast"), a tier ("mid" or "tier:mid"), or a
          plain model id. Alias chains are followed (with a cycle guard).
        - A tier resolves to the best live model at that tier, or None if the
          index has none.
        - A plain model id passes through unchanged.
        """
        v = value
        seen: set[str] = set()
        while v and v.lower() in self._aliases and v.lower() not in seen:
            seen.add(v.lower())
            v = self._aliases[v.lower()]
        name = v[5:] if v.lower().startswith("tier:") else v
        t = TIER_BY_NAME.get(name.lower()) if name else None
        if t is not None:
            models = self._models_at_tier(t)
            return models[0] if models else None
        return v

    def resolve_full(
        self, model: str | None, provider_hint: str | None, floor: int | None
    ) -> Resolution | None:
        """Request-time resolution with defaults, aliases, and a provider hint.

        Precedence is 'model wins, provider is a hint':
          - An explicit model routes normally; a provider hint only reorders the
            candidate providers (or, if the model is otherwise unroutable, picks
            the hinted provider).
          - A missing/`default`/`auto` model falls back to the provider's default
            (when a provider is named) or the global default.

        Returns None only when no model can be determined at all (no model given
        and no default configured). An undetermined-but-named model comes back as
        a Resolution with empty providers (the caller turns that into a 404).
        """
        hint = provider_hint.lower() if provider_hint else None
        chosen = model
        if not chosen or chosen.lower() in ("default", "auto"):
            chosen = (self._provider_defaults.get(hint) if hint else None) or self._global_default
            if not chosen:
                return None

        concrete = self.materialize(chosen) or chosen
        res = self.resolve(concrete, floor=floor)

        if hint:
            if hint in res.providers:
                res.providers = [hint] + [p for p in res.providers if p != hint]
            elif not res.providers and hint in self._known_ids:
                # Model didn't resolve on its own; honor the hint as the target.
                res = Resolution(concrete, [hint], "provider-hint", tier=res.tier, floor=res.floor)
        return res

    def merge_provider_models(self, provider: str, models: list[str]) -> None:
        """Replace one provider's slice of the live index with `models`, then persist."""
        for m in list(self._index):
            self._index[m].discard(provider)
        for m in models:
            self._index.setdefault(m, set()).add(provider)
        self._index = {m: ps for m, ps in self._index.items() if ps}
        self._index_built_at = time.time()
        self._rebuild_tier_index()
        self._save_cached_index()

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
