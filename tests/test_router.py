from synckey.tiers import FRONTIER, HIGH, MID


def test_resolve_explicit_prefix(ctx):
    res = ctx.router.resolve("groq/llama-3.3-70b-versatile")
    assert res.how == "prefix"
    assert res.providers == ["groq"]
    assert res.bare_model == "llama-3.3-70b-versatile"


def test_resolve_pattern(ctx):
    res = ctx.router.resolve("gemini-2.0-flash")
    assert res.how == "pattern"
    assert res.providers == ["gemini"]


def test_resolve_index_ordered_by_priority(ctx):
    ctx.router._index = {"llama-3.3-70b": {"groq", "cerebras"}}
    ctx.router._rebuild_tier_index()
    ctx.settings.provider_priority = ["cerebras", "groq"]
    res = ctx.router.resolve("llama-3.3-70b")
    assert res.how == "index"
    assert res.providers == ["cerebras", "groq"]


def test_resolve_unknown(ctx):
    res = ctx.router.resolve("totally-made-up-xyz")
    assert res.how == "unknown"
    assert res.providers == []


def test_tier_set_for_known_models(ctx):
    res = ctx.router.resolve("claude-opus-4-8")
    assert res.tier == FRONTIER

    res = ctx.router.resolve("gpt-4o-mini")
    assert res.tier == MID


def test_floor_equals_model_tier_by_default(ctx):
    res = ctx.router.resolve("claude-opus-4-8")
    assert res.floor == FRONTIER  # floor auto-set to model's tier


def test_floor_override(ctx):
    res = ctx.router.resolve("claude-opus-4-8", floor=HIGH)
    assert res.floor == HIGH  # caller explicitly lowered the floor


def test_tier_alternatives_excludes_primary(ctx):
    ctx.router._index = {
        "claude-opus-4-8": {"anthropic"},
        "gpt-5": {"openai"},
    }
    ctx.router._rebuild_tier_index()
    alts = ctx.router.tier_alternatives(FRONTIER, FRONTIER, "claude-opus-4-8")
    models = [m for m, _ in alts]
    assert "claude-opus-4-8" not in models
    assert "gpt-5" in models


def test_tier_alternatives_respects_floor(ctx):
    ctx.router._index = {
        "claude-sonnet-4-6": {"anthropic"},  # HIGH
        "gpt-4o-mini": {"openai"},           # MID
    }
    ctx.router._rebuild_tier_index()
    # Floor is HIGH, so MID alternatives should not appear.
    alts = ctx.router.tier_alternatives(HIGH, HIGH, "claude-sonnet-4-6")
    models = [m for m, _ in alts]
    assert "gpt-4o-mini" not in models
