"""User-managed routing preferences.

These are the knobs a user sets interactively (via `synckey setup`, `config`,
`tier`, or the `key add` flow) rather than hand-editing a file:

    global_default_model   model used when a request names neither model nor provider
    provider_defaults      {provider: model} used when a request names only a provider
    aliases                {name: target}   target is a model id or a tier name
    tier_overrides         {model: tier}    manual tier assignment

They live in DB meta (JSON) so they survive restarts and are editable
programmatically. config.toml stays the home for *static* infra (host/port,
custom providers, price overrides) that you version-control; these are the
*dynamic* choices you tune as you go. `synckey config` shows both in one place.
"""

from __future__ import annotations

from .db import Database

GLOBAL_DEFAULT = "global_default_model"
PROVIDER_DEFAULTS = "provider_defaults"
ALIASES = "aliases"
TIER_OVERRIDES = "tier_overrides_db"


class Prefs:
    def __init__(self, db: Database):
        self.db = db

    # global default
    def global_default(self) -> str | None:
        return self.db.get_meta(GLOBAL_DEFAULT)

    def set_global_default(self, model: str) -> None:
        self.db.set_meta(GLOBAL_DEFAULT, model)

    # per-provider defaults
    def provider_defaults(self) -> dict[str, str]:
        return self.db.get_json(PROVIDER_DEFAULTS, {}) or {}

    def provider_default(self, provider: str) -> str | None:
        return self.provider_defaults().get(provider.lower())

    def set_provider_default(self, provider: str, model: str) -> None:
        d = self.provider_defaults()
        d[provider.lower()] = model
        self.db.set_json(PROVIDER_DEFAULTS, d)

    # aliases
    def aliases(self) -> dict[str, str]:
        return self.db.get_json(ALIASES, {}) or {}

    def set_alias(self, name: str, target: str) -> None:
        a = self.aliases()
        a[name.lower()] = target
        self.db.set_json(ALIASES, a)

    def remove_alias(self, name: str) -> bool:
        a = self.aliases()
        if name.lower() in a:
            del a[name.lower()]
            self.db.set_json(ALIASES, a)
            return True
        return False

    # tier overrides
    def tier_overrides(self) -> dict[str, str]:
        return self.db.get_json(TIER_OVERRIDES, {}) or {}

    def set_tier_override(self, model: str, tier: str) -> None:
        t = self.tier_overrides()
        t[model.lower()] = tier
        self.db.set_json(TIER_OVERRIDES, t)

    def remove_tier_override(self, model: str) -> bool:
        t = self.tier_overrides()
        if model.lower() in t:
            del t[model.lower()]
            self.db.set_json(TIER_OVERRIDES, t)
            return True
        return False
