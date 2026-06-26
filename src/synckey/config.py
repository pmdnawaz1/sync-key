"""Configuration & paths.

State lives under ``$SYNCKEY_HOME`` (default ``~/.synckey``):

    secret.key      Fernet key for sealing provider credentials (0600)
    synckey.db      SQLite: keys, usage, runtime key state, metadata
    config.toml     User settings + custom providers

The unified gateway key is generated once at ``init`` time; only its SHA-256
hash is persisted, so the plaintext is shown exactly once.
"""

from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

UNIFIED_KEY_PREFIX = "sk-synckey-"


def home() -> Path:
    return Path(os.environ.get("SYNCKEY_HOME", Path.home() / ".synckey"))


def secret_key_path() -> Path:
    return home() / "secret.key"


def db_path() -> Path:
    return Path(os.environ.get("SYNCKEY_DB", home() / "synckey.db"))


def config_path() -> Path:
    return home() / "config.toml"


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    # Routing: ordered preference when several providers serve the same model.
    provider_priority: list[str] = field(default_factory=list)
    # Allow falling back to a *different* provider when every key for the
    # detected provider is rate-limited / failing.
    cross_provider_fallback: bool = True
    # Per-request retry budget across the whole key pool.
    max_retries: int = 4
    # Default cooldown (seconds) applied to a key on 429 with no Retry-After.
    default_cooldown: float = 20.0
    request_timeout: float = 120.0
    custom_providers: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        path = config_path()
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text())
        gw = data.get("gateway", {})
        routing = data.get("routing", {})
        return cls(
            host=gw.get("host", cls.host),
            port=gw.get("port", cls.port),
            request_timeout=gw.get("request_timeout", cls.request_timeout),
            provider_priority=routing.get("priority", []),
            cross_provider_fallback=routing.get("cross_provider_fallback", True),
            max_retries=routing.get("max_retries", cls.max_retries),
            default_cooldown=routing.get("default_cooldown", cls.default_cooldown),
            custom_providers=data.get("providers", []),
        )


def ensure_home() -> Path:
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(h, 0o700)
    except OSError:  # pragma: no cover - non-posix
        pass
    return h


def generate_unified_key() -> str:
    return UNIFIED_KEY_PREFIX + secrets.token_urlsafe(32)
