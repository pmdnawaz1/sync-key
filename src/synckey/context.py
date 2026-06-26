"""Shared runtime wiring."""

from __future__ import annotations

import os

from .config import Settings, db_path, secret_key_path
from .crypto import SecretBox
from .db import Database
from .keypool import KeyPool
from .prefs import Prefs
from .providers import BUILTIN, Provider
from .router import Router
from .state import StateStore
from .tiers import load_overrides


def merged_providers(settings: Settings) -> dict[str, Provider]:
    providers = dict(BUILTIN)
    for spec in settings.custom_providers:
        try:
            prov = Provider(
                id=spec["id"],
                name=spec.get("name", spec["id"]),
                base_url=spec["base_url"],
                models_path=spec.get("models_path", "/models"),
                env=tuple(spec.get("env", ())),
                patterns=tuple(spec.get("patterns", ())),
                auth=spec.get("auth", "bearer"),
                signup=spec.get("signup", ""),
                notes=spec.get("notes", ""),
            )
            providers[prov.id] = prov
        except KeyError:
            continue
    return providers


class Context:
    def __init__(self) -> None:
        self.settings = Settings.load()
        self.providers = merged_providers(self.settings)
        self.box = SecretBox(secret_key_path())
        self.db = Database(db_path())
        self.prefs = Prefs(self.db)
        # Tier overrides come from two places: config.toml (static, versioned) and
        # the DB (set via `synckey tier set`). DB wins on conflict.
        merged_tiers = {**self.settings.tier_overrides, **self.prefs.tier_overrides()}
        load_overrides(merged_tiers, self.settings.price_overrides)
        self.states = StateStore(self.db.path)
        self.pool = KeyPool(
            self.db,
            self.box,
            self.states,
            default_cooldown=self.settings.default_cooldown,
        )
        self.router = Router(self.db, self.providers, self.settings, self.prefs)

    def first_secret(self, provider_id: str) -> str | None:
        keys = self.db.list_keys(provider=provider_id, enabled_only=True)
        if keys:
            return self.box.open(keys[0].secret)
        prov = self.providers.get(provider_id)
        if prov:
            for env in prov.env:
                if os.environ.get(env):
                    return os.environ[env]
        return None

    def unified_key_hash(self) -> str | None:
        return self.db.get_meta("unified_key_hash")

    def close(self) -> None:
        self.states.close()
        self.db.close()
