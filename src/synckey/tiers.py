"""Model tier catalog, pricing, and tier-floored fallback logic.

Tiers:
    FRONTIER (4)  best quality, most expensive (Opus, GPT-5, o3)
    HIGH     (3)  capable, premium (Sonnet, GPT-4o, o1)
    MID      (2)  fast, affordable (Haiku, GPT-4o-mini, Flash, 70b)
    LOW      (1)  budget, high-volume (8b, Gemma, Qwen)

A request for a frontier model will never fall back below FRONTIER unless the
caller explicitly sets X-Quality-Floor: high (or lower). This is the Opus ->
GPT-5 guarantee, never Opus -> oss-llama.
"""

from __future__ import annotations

import re

FRONTIER = 4
HIGH     = 3
MID      = 2
LOW      = 1

TIER_NAMES = {FRONTIER: "frontier", HIGH: "high", MID: "mid", LOW: "low"}
TIER_BY_NAME = {v: k for k, v in TIER_NAMES.items()}

# (pattern, tier) -- checked in order; more-specific patterns first.
# gpt-4o-mini must come before gpt-4o to avoid premature match.
_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"claude-opus",         re.I), FRONTIER),
    (re.compile(r"gpt-5",               re.I), FRONTIER),
    (re.compile(r"^o3",                 re.I), FRONTIER),
    (re.compile(r"gemini-2\.5-pro",     re.I), FRONTIER),
    (re.compile(r"grok-3",              re.I), FRONTIER),
    (re.compile(r"magistral",           re.I), FRONTIER),

    (re.compile(r"claude-sonnet",       re.I), HIGH),
    (re.compile(r"gpt-4\.1(?!-mini)",   re.I), HIGH),
    (re.compile(r"gpt-4o(?!-mini)",     re.I), HIGH),
    (re.compile(r"^o1",                 re.I), HIGH),
    (re.compile(r"gemini-2\.0-pro",     re.I), HIGH),
    (re.compile(r"gemini-1\.5-pro",     re.I), HIGH),
    (re.compile(r"mistral-large",       re.I), HIGH),
    (re.compile(r"grok-2",              re.I), HIGH),
    (re.compile(r"deepseek-r1",         re.I), HIGH),

    (re.compile(r"claude-haiku",        re.I), MID),
    (re.compile(r"gpt-4o-mini",         re.I), MID),
    (re.compile(r"gpt-4\.1-mini",       re.I), MID),
    (re.compile(r"gemini-2\.0-flash",   re.I), MID),
    (re.compile(r"gemini-flash",        re.I), MID),
    (re.compile(r"llama-3\.3-70b",      re.I), MID),
    (re.compile(r"llama-3\.1-70b",      re.I), MID),
    (re.compile(r"llama-3-70b",         re.I), MID),
    (re.compile(r"mistral-medium",      re.I), MID),
    (re.compile(r"codestral",           re.I), MID),
    (re.compile(r"deepseek-chat",       re.I), MID),
    (re.compile(r"command-r-plus",      re.I), MID),
    (re.compile(r"command-r(?!-plus)",  re.I), MID),
    (re.compile(r"qwen-72b",            re.I), MID),
    (re.compile(r"mixtral",             re.I), MID),
    (re.compile(r"llama3\.1-70b",       re.I), MID),

    (re.compile(r"gemma",               re.I), LOW),
    (re.compile(r"llama-3\.1-8b",       re.I), LOW),
    (re.compile(r"llama-3-8b",          re.I), LOW),
    (re.compile(r"llama3\.1-8b",        re.I), LOW),
    (re.compile(r"qwen(?!-72b)",        re.I), LOW),
    (re.compile(r"mistral-7b",          re.I), LOW),
    (re.compile(r"mistral-0",           re.I), LOW),
    (re.compile(r"command-light",       re.I), LOW),
    (re.compile(r"gemini-1\.5-flash-8", re.I), LOW),
    (re.compile(r"phi-3",               re.I), LOW),
]

# (input $/M tokens, output $/M tokens) -- best-effort, users can override
_PRICES: list[tuple[re.Pattern, tuple[float, float]]] = [
    (re.compile(r"claude-opus-4-8",     re.I), (15.00, 75.00)),
    (re.compile(r"claude-opus",         re.I), (15.00, 75.00)),
    (re.compile(r"claude-sonnet-4-6",   re.I), ( 3.00, 15.00)),
    (re.compile(r"claude-sonnet",       re.I), ( 3.00, 15.00)),
    (re.compile(r"claude-haiku-4-5",    re.I), ( 0.80,  4.00)),
    (re.compile(r"claude-haiku",        re.I), ( 0.80,  4.00)),
    (re.compile(r"gpt-5",               re.I), (10.00, 40.00)),
    (re.compile(r"gpt-4\.1-mini",       re.I), ( 0.40,  1.60)),
    (re.compile(r"gpt-4\.1",            re.I), ( 2.00,  8.00)),
    (re.compile(r"gpt-4o-mini",         re.I), ( 0.15,  0.60)),
    (re.compile(r"gpt-4o",              re.I), ( 5.00, 15.00)),
    (re.compile(r"^o3",                 re.I), (10.00, 40.00)),
    (re.compile(r"^o1-mini",            re.I), ( 3.00, 12.00)),
    (re.compile(r"^o1",                 re.I), (15.00, 60.00)),
    (re.compile(r"gemini-2\.5-pro",     re.I), ( 3.50, 10.50)),
    (re.compile(r"gemini-2\.0-flash",   re.I), ( 0.10,  0.40)),
    (re.compile(r"gemini-flash",        re.I), ( 0.10,  0.40)),
    (re.compile(r"gemini-1\.5-pro",     re.I), ( 3.50, 10.50)),
    (re.compile(r"gemini-1\.5-flash",   re.I), ( 0.075, 0.30)),
    (re.compile(r"llama-3\.3-70b",      re.I), ( 0.59,  0.79)),
    (re.compile(r"llama-3\.1-70b",      re.I), ( 0.52,  0.75)),
    (re.compile(r"llama-3\.1-8b",       re.I), ( 0.05,  0.08)),
    (re.compile(r"llama3\.1-70b",       re.I), ( 0.52,  0.75)),
    (re.compile(r"llama3\.1-8b",        re.I), ( 0.05,  0.08)),
    (re.compile(r"mistral-large",       re.I), ( 3.00,  9.00)),
    (re.compile(r"mistral-medium",      re.I), ( 2.70,  8.10)),
    (re.compile(r"codestral",           re.I), ( 0.30,  0.90)),
    (re.compile(r"deepseek-r1",         re.I), ( 0.55,  2.19)),
    (re.compile(r"deepseek-chat",       re.I), ( 0.27,  1.10)),
    (re.compile(r"command-r-plus",      re.I), ( 2.50, 10.00)),
    (re.compile(r"command-r",           re.I), ( 0.15,  0.60)),
    (re.compile(r"grok-3",              re.I), ( 3.00, 15.00)),
    (re.compile(r"grok-2",              re.I), ( 2.00, 10.00)),
    (re.compile(r"mixtral",             re.I), ( 0.24,  0.24)),
    (re.compile(r"gemma",               re.I), ( 0.10,  0.10)),
]

# User-configurable overrides; set by load_overrides().
_tier_overrides: dict[str, int] = {}
_price_overrides: dict[str, tuple[float, float]] = {}


def load_overrides(tiers: dict[str, str], prices: dict[str, list]) -> None:
    """Load user-defined tier and price overrides from config."""
    for model, tier_name in tiers.items():
        t = TIER_BY_NAME.get(tier_name.lower())
        if t is not None:
            _tier_overrides[model.lower()] = t
    for model, pair in prices.items():
        if len(pair) == 2:
            _price_overrides[model.lower()] = (float(pair[0]), float(pair[1]))


def model_tier(model: str) -> int | None:
    """Return the capability tier for a model name, or None if unknown."""
    key = model.lower()
    if key in _tier_overrides:
        return _tier_overrides[key]
    for rx, tier in _PATTERNS:
        if rx.search(model):
            return tier
    return None


def model_price(model: str) -> tuple[float, float] | None:
    """Return (input, output) price in $/M tokens, or None if unknown."""
    key = model.lower()
    if key in _price_overrides:
        return _price_overrides[key]
    for rx, price in _PRICES:
        if rx.search(model):
            return price
    return None


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Compute cost in USD for a completed request, or None if price unknown."""
    price = model_price(model)
    if price is None:
        return None
    return (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000
