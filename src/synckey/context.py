"""Shared runtime wiring."""

from __future__ import annotations

import os

from .bucket import BucketRegistry
from .config import Settings, db_path, secret_key_path
from .crypto import SecretBox
from .db import Database
from .keypool import KeyPool
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
        load_overrides(self.settings.tier_overrides, self.settings.price_overrides)
        self.providers = merged_providers(self.settings)
        self.box = SecretBox(secret_key_path())
        self.db = Database(db_path())
        self.states = StateStore(self.db.path)
        self.pool = KeyPool(
            self.db,
            self.box,
            self.states,
            default_cooldown=self.settings.default_cooldown,
        )
        self.router = Router(self.db, self.providers, self.settings)

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
