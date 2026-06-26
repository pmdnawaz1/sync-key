import os
import tempfile

import pytest


@pytest.fixture()
def synckey_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="synckey-test-")
    monkeypatch.setenv("SYNCKEY_HOME", d)
    monkeypatch.delenv("SYNCKEY_DB", raising=False)
    yield d


@pytest.fixture()
def ctx(synckey_home):
    from synckey.context import Context
    from synckey.db import sha256

    c = Context()
    c.db.set_meta("unified_key_hash", sha256("sk-synckey-test"))
    yield c
    c.close()
