import time


def _add(ctx, provider, label, secret, weight=1):
    return ctx.db.add_key(provider, label, ctx.box.seal(secret), weight=weight)


def test_round_robin_rotates(ctx):
    a = _add(ctx, "groq", "a", "key-a")
    b = _add(ctx, "groq", "b", "key-b")
    first = ctx.pool.candidates("groq")[0].key_id
    second = ctx.pool.candidates("groq")[0].key_id
    assert {first, second} == {a, b}
    assert first != second  # rotation advanced


def test_rate_limited_key_goes_to_back(ctx):
    a = _add(ctx, "groq", "a", "key-a")
    b = _add(ctx, "groq", "b", "key-b")
    ctx.pool.report_rate_limit(a, retry_after=60)
    cands = ctx.pool.candidates("groq")
    # b (live) must come before a (cooling)
    assert cands[0].key_id == b
    assert cands[0].cooling is False
    assert cands[-1].key_id == a
    assert cands[-1].cooling is True


def test_all_cooling_still_returns_soonest_first(ctx):
    a = _add(ctx, "groq", "a", "key-a")
    b = _add(ctx, "groq", "b", "key-b")
    ctx.pool.report_rate_limit(a, retry_after=120)
    ctx.pool.report_rate_limit(b, retry_after=5)
    cands = ctx.pool.candidates("groq")
    assert all(c.cooling for c in cands)
    assert cands[0].key_id == b  # frees up soonest


def test_weight_expands_share(ctx):
    a = _add(ctx, "groq", "a", "key-a", weight=3)
    _add(ctx, "groq", "b", "key-b", weight=1)
    picks = [ctx.pool.candidates("groq")[0].key_id for _ in range(8)]
    assert picks.count(a) > picks.count(picks[-1]) or picks.count(a) >= 4


def test_secret_is_decrypted(ctx):
    _add(ctx, "groq", "a", "super-secret")
    assert ctx.pool.candidates("groq")[0].secret == "super-secret"


def test_disabled_key_excluded(ctx):
    a = _add(ctx, "groq", "a", "key-a")
    ctx.db.set_key_enabled(a, False)
    assert ctx.pool.candidates("groq") == []
