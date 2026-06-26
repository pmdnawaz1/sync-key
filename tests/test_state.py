import time
import tempfile
from pathlib import Path

import pytest

from synckey.state import StateStore, Health


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "synckey.db"
    s = StateStore(db)
    yield s
    s.close()


def test_default_live(store):
    assert store.get(1).health == Health.LIVE
    assert store.get(1).is_available()


def test_cooling(store):
    store.set_cooling(1, time.time() + 60)
    st = store.get(1)
    assert st.health == Health.COOLING
    assert not st.is_available()
    assert st.cooldown_remaining() > 55


def test_cooling_auto_recovers_on_reload(tmp_path):
    db = tmp_path / "synckey.db"
    s = StateStore(db)
    s.set_cooling(42, time.time() - 1)  # already expired
    s.close()
    s2 = StateStore(db)
    # Expired cooldown should be loaded as LIVE
    assert s2.get(42).health == Health.LIVE
    s2.close()


def test_dead(store):
    store.set_dead(2, "401 bad key")
    st = store.get(2)
    assert st.health == Health.DEAD
    assert not st.is_available()
    assert st.dead_reason == "401 bad key"


def test_dead_persists_across_restart(tmp_path):
    db = tmp_path / "synckey.db"
    s = StateStore(db)
    s.set_dead(7, "expired")
    s.close()
    s2 = StateStore(db)
    assert s2.get(7).health == Health.DEAD
    assert s2.get(7).dead_reason == "expired"
    s2.close()


def test_set_live_clears_cooling(store):
    store.set_cooling(3, time.time() + 100)
    store.set_live(3)
    assert store.get(3).health == Health.LIVE
    assert store.get(3).is_available()


def test_cooldown_durability(tmp_path):
    db = tmp_path / "synckey.db"
    s = StateStore(db)
    s.set_cooling(5, time.time() + 300)
    s.close()
    s2 = StateStore(db)
    st = s2.get(5)
    assert st.health == Health.COOLING
    assert not st.is_available()
    assert st.cooldown_remaining() > 290
    s2.close()
