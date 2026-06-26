import time

import pytest

from synckey.state import Health


def _add(ctx, provider, label, secret, weight=1):
    return ctx.db.add_key(provider, label, ctx.box.seal(secret), weight=weight)


def test_round_robin_rotates(ctx):
    a = _add(ctx, "groq", "a", "key-a")
    b = _add(ctx, "groq", "b", "key-b")
    first = ctx.pool.candidates("groq")[0].key_id
    second = ctx.pool.candidates("groq")[0].key_id
    assert {first, second} == {a, b}
    assert first != second


def test_rate_limited_goes_to_back(ctx):
    a = _add(ctx, "groq", "a", "key-a")
    b = _add(ctx, "groq", "b", "key-b")
    ctx.pool.on_rate_limit(a, retry_after=60)
    cands = ctx.pool.candidates("groq")
    # b (live) must precede a (cooling)
    live_ids = [c.key_id for c in cands if not c.cooling]
    cool_ids = [c.key_id for c in cands if c.cooling]
    assert live_ids == [b]
    assert cool_ids == [a]


def test_dead_key_excluded(ctx):
    a = _add(ctx, "groq", "a", "key-a")
    b = _add(ctx, "groq", "b", "key-b")
    ctx.pool.on_dead(a, "401")
    cands = ctx.pool.candidates("groq")
    assert all(c.key_id != a for c in cands)
    assert any(c.key_id == b for c in cands)


def test_all_cooling_ordered_by_soonest(ctx):
    a = _add(ctx, "groq", "a", "key-a")
    b = _add(ctx, "groq", "b", "key-b")
    ctx.pool.on_rate_limit(a, retry_after=120)
    ctx.pool.on_rate_limit(b, retry_after=5)
    cands = ctx.pool.candidates("groq")
    assert all(c.cooling for c in cands)
    # b frees up sooner
    assert cands[0].key_id == b


def test_disabled_key_excluded(ctx):
    a = _add(ctx, "groq", "a", "key-a")
    ctx.db.set_key_enabled(a, False)
    assert ctx.pool.candidates("groq") == []


def test_secret_decrypted(ctx):
    _add(ctx, "groq", "a", "super-secret")
    assert ctx.pool.candidates("groq")[0].secret == "super-secret"


def test_dead_state_persists(ctx):
    a = _add(ctx, "groq", "a", "key-a")
    ctx.pool.on_dead(a, "expired")
    # Simulate restart: create a new pool from the same db
    from synckey.keypool import KeyPool
    from synckey.state import StateStore
    new_states = StateStore(ctx.db.path)
    new_pool = KeyPool(ctx.db, ctx.box, new_states)
    cands = new_pool.candidates("groq")
    # Dead key must still be excluded after restart
    assert all(c.key_id != a for c in cands)
    new_states.close()


def test_proactive_throttle_skips_exhausted_bucket(ctx):
    a = _add(ctx, "groq", "a", "key-a")
    b = _add(ctx, "groq", "b", "key-b")
    # Exhaust a's bucket completely.
    bucket_a = ctx.pool._buckets.get(a, rpm_cap=1)
    bucket_a.rpm_cap = 1
    bucket_a._req_tokens = 0
    cands = ctx.pool.candidates("groq")
    # a is throttled (not cooling), b is ready
    ready = [c for c in cands if c.ready]
    throttled = [c for c in cands if not c.ready and not c.cooling]
    assert any(c.key_id == b for c in ready)
    assert any(c.key_id == a for c in throttled)
