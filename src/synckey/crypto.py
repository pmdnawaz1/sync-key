"""At-rest encryption for stored provider secrets.

One symmetric key lives in ~/.synckey/secret.key with 0600 perms. Provider
credentials are sealed with Fernet so the database never holds plaintext. Lose
the secret file and the stored credentials are unrecoverable by design.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key_path: Path):
        self.key_path = key_path
        self._fernet = Fernet(self._load_or_create())

    def _load_or_create(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key

    def seal(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def open(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError(
                "Could not decrypt a stored secret. The secret.key file may have "
                "changed. Re-add affected keys with `synckey key add`."
            ) from exc
