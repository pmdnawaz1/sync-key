"""Defaults, aliases, tier overrides, and the 'model wins, provider is a hint' contract."""

from synckey.prefs import Prefs
from synckey.tiers import FRONTIER, MID


def _index(ctx, mapping):
    ctx.router._index = {m: set(ps) for m, ps in mapping.items()}
    ctx.router._rebuild_tier_index()


# ---- prefs storage ----------------------------------------------------------

def test_prefs_roundtrip(ctx):
    p = Prefs(ctx.db)
    p.set_global_default("gemini-2.0-flash")
    p.set_provider_default("groq", "llama-3.3-70b-versatile")
    p.set_alias("fast", "mid")
    assert p.global_default() == "gemini-2.0-flash"
    assert p.provider_default("groq") == "llama-3.3-70b-versatile"
    assert p.aliases()["fast"] == "mid"
    assert p.remove_alias("fast") is True
    assert "fast" not in p.aliases()


# ---- materialize (alias + tier expansion) -----------------------------------

def test_materialize_plain_passthrough(ctx):
    assert ctx.router.materialize("gemini-2.0-flash") == "gemini-2.0-flash"


def test_materialize_alias_chain(ctx):
    ctx.prefs.set_alias("fast", "cheap")
    ctx.prefs.set_alias("cheap", "gemini-2.0-flash")
    ctx.router.reload_prefs()
    assert ctx.router.materialize("fast") == "gemini-2.0-flash"


def test_materialize_tier_to_live_model(ctx):
    _index(ctx, {"gpt-4o-mini": {"openai"}})  # MID
    assert ctx.router.materialize("mid") == "gpt-4o-mini"
    assert ctx.router.materialize("tier:mid") == "gpt-4o-mini"


def test_materialize_tier_empty_index_returns_none(ctx):
    _index(ctx, {})
    assert ctx.router.materialize("frontier") is None


# ---- resolve_full -----------------------------------------------------------

def test_global_default_used_when_no_model(ctx):
    ctx.prefs.set_global_default("gemini-2.0-flash")
    ctx.router.reload_prefs()
    res = ctx.router.resolve_full(None, None, None)
    assert res is not None
    assert res.bare_model == "gemini-2.0-flash"
    assert res.providers == ["gemini"]


def test_default_keyword_uses_default(ctx):
    ctx.prefs.set_global_default("gemini-2.0-flash")
    ctx.router.reload_prefs()
    res = ctx.router.resolve_full("default", None, None)
    assert res.bare_model == "gemini-2.0-flash"


def test_provider_default_used_when_only_provider(ctx):
    ctx.prefs.set_global_default("gemini-2.0-flash")
    ctx.prefs.set_provider_default("groq", "llama-3.3-70b-versatile")
    ctx.router.reload_prefs()
    res = ctx.router.resolve_full(None, "groq", None)
    assert res.bare_model == "llama-3.3-70b-versatile"
    assert res.providers == ["groq"]


def test_no_model_no_default_returns_none(ctx):
    res = ctx.router.resolve_full(None, None, None)
    assert res is None


def test_provider_hint_reorders_not_overrides(ctx):
    _index(ctx, {"llama-3.3-70b": {"groq", "cerebras"}})
    ctx.settings.provider_priority = ["cerebras", "groq"]
    # Without hint: cerebras first (priority).
    assert ctx.router.resolve_full("llama-3.3-70b", None, None).providers[0] == "cerebras"
    # With hint groq: groq jumps to front, but cerebras still present.
    res = ctx.router.resolve_full("llama-3.3-70b", "groq", None)
    assert res.providers[0] == "groq"
    assert "cerebras" in res.providers


def test_provider_hint_for_unroutable_model(ctx):
    res = ctx.router.resolve_full("some-unknown-model", "groq", None)
    assert res.providers == ["groq"]
    assert res.how == "provider-hint"


def test_explicit_model_wins_over_provider_hint(ctx):
    # gemini-2.0-flash routes to gemini by pattern; a groq hint that can't serve
    # it is ignored (model wins).
    res = ctx.router.resolve_full("gemini-2.0-flash", "groq", None)
    assert res.providers == ["gemini"]


def test_merge_provider_models_updates_index(ctx):
    ctx.router.merge_provider_models("groq", ["llama-3.3-70b-versatile", "gpt-oss-120b"])
    assert "groq" in ctx.router.index["llama-3.3-70b-versatile"]
    ctx.router.merge_provider_models("groq", ["llama-3.3-70b-versatile"])
    # gpt-oss-120b no longer served by groq -> dropped from index.
    assert "gpt-oss-120b" not in ctx.router.index
