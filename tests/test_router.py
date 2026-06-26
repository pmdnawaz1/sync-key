def test_resolve_explicit_prefix(ctx):
    res = ctx.router.resolve("groq/llama-3.3-70b-versatile")
    assert res.how == "prefix"
    assert res.providers == ["groq"]
    assert res.bare_model == "llama-3.3-70b-versatile"


def test_resolve_pattern(ctx):
    res = ctx.router.resolve("gemini-2.0-flash")
    assert res.how == "pattern"
    assert res.providers == ["gemini"]


def test_resolve_index_takes_priority_and_orders(ctx):
    # Two providers advertise the same model via the live index.
    ctx.router._index = {"llama-3.3-70b": {"groq", "cerebras"}}
    ctx.settings.provider_priority = ["cerebras", "groq"]
    res = ctx.router.resolve("llama-3.3-70b")
    assert res.how == "index"
    assert res.providers == ["cerebras", "groq"]


def test_resolve_unknown(ctx):
    res = ctx.router.resolve("totally-made-up-model-xyz")
    assert res.how == "unknown"
    assert res.providers == []
