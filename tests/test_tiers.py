from synckey.tiers import (
    FRONTIER, HIGH, MID, LOW,
    model_tier, model_price, cost_usd, load_overrides,
)


def test_frontier_models():
    assert model_tier("claude-opus-4-8") == FRONTIER
    assert model_tier("gpt-5") == FRONTIER
    assert model_tier("o3") == FRONTIER


def test_high_models():
    assert model_tier("claude-sonnet-4-6") == HIGH
    assert model_tier("gpt-4o") == HIGH
    assert model_tier("o1") == HIGH


def test_mid_models():
    assert model_tier("claude-haiku-4-5") == MID
    assert model_tier("gpt-4o-mini") == MID
    assert model_tier("gemini-2.0-flash") == MID
    assert model_tier("llama-3.3-70b-versatile") == MID


def test_low_models():
    assert model_tier("gemma-3-27b") == LOW
    assert model_tier("llama-3.1-8b-instant") == LOW


def test_gpt4o_vs_mini_no_confusion():
    assert model_tier("gpt-4o") == HIGH
    assert model_tier("gpt-4o-mini") == MID


def test_unknown_model():
    assert model_tier("unknown-xyz-999") is None


def test_user_override():
    load_overrides({"my-custom-model": "frontier"}, {})
    assert model_tier("my-custom-model") == FRONTIER


def test_pricing():
    price = model_price("claude-opus-4-8")
    assert price is not None
    assert price[0] == 15.0
    assert price[1] == 75.0


def test_cost_usd():
    c = cost_usd("claude-sonnet-4-6", 1000, 500)
    assert c is not None
    # 1000 * 3/1M + 500 * 15/1M = 0.003 + 0.0075 = 0.0105
    assert abs(c - 0.0105) < 0.0001


def test_cost_unknown_model():
    assert cost_usd("unknown-model-xyz", 100, 50) is None
