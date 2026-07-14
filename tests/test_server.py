import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from synckey.server import create_app

AUTH = {"Authorization": "Bearer sk-synckey-test"}
GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"


def _add(ctx, provider="groq", n=2):
    return [ctx.db.add_key(provider, f"k{i}", ctx.box.seal(f"secret-{i}")) for i in range(n)]


def test_auth_required(ctx):
    with TestClient(create_app(ctx)) as client:
        r = client.post("/v1/chat/completions", json={"model": "groq/x"})
        assert r.status_code == 401


def test_unroutable_model_404(ctx):
    with TestClient(create_app(ctx)) as client:
        r = client.post(
            "/v1/chat/completions", headers=AUTH, json={"model": "no-such-model-anywhere"}
        )
        assert r.status_code == 404


@respx.mock
def test_success_records_usage_and_cost(ctx):
    _add(ctx)
    respx.post(GROQ_CHAT).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )
    )
    with TestClient(create_app(ctx)) as client:
        r = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "groq/llama-3.3-70b-versatile", "messages": []},
        )
    assert r.status_code == 200
    # Usage written to DB (flush may be async; check after brief settle)
    import time; time.sleep(0.7)
    totals = ctx.db.usage_totals()
    assert totals["requests"] == 1
    assert totals["total_tokens"] == 15


@respx.mock
def test_429_eaten_failover_to_second_key(ctx):
    _add(ctx, n=2)
    route = respx.post(GROQ_CHAT)
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "30"}, json={"error": "slow down"}),
        httpx.Response(
            200,
            json={"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        ),
    ]
    with TestClient(create_app(ctx)) as client:
        r = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "groq/llama-3.3-70b-versatile", "messages": []},
        )
    assert r.status_code == 200
    assert route.call_count == 2


@respx.mock
def test_dead_key_after_401(ctx):
    ids = _add(ctx, n=1)
    respx.post(GROQ_CHAT).mock(return_value=httpx.Response(401, json={"error": "invalid key"}))
    with TestClient(create_app(ctx)) as client:
        client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "groq/llama-3.3-70b-versatile", "messages": []},
        )
    from synckey.state import Health
    assert ctx.states.get(ids[0]).health == Health.DEAD


@respx.mock
def test_401_marks_dead_and_event_logged(ctx):
    ids = _add(ctx, n=1)
    respx.post(GROQ_CHAT).mock(return_value=httpx.Response(401, json={"error": "bad"}))
    with TestClient(create_app(ctx)) as client:
        client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "groq/llama-3.3-70b-versatile", "messages": []},
        )
    events = ctx.db.recent_events(10, "key_dead")
    assert len(events) >= 1


@respx.mock
def test_4xx_not_retried(ctx):
    _add(ctx, n=2)
    route = respx.post(GROQ_CHAT).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad request"}})
    )
    with TestClient(create_app(ctx)) as client:
        r = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "groq/llama-3.3-70b-versatile", "messages": []},
        )
    assert r.status_code == 400
    assert route.call_count == 1


def test_models_endpoint(ctx):
    ctx.router._index = {"gemini-2.0-flash": {"gemini"}}
    with TestClient(create_app(ctx)) as client:
        r = client.get("/v1/models", headers=AUTH)
    assert r.status_code == 200
    data = r.json()["data"]
    assert any(m["id"] == "gemini-2.0-flash" for m in data)
    # Tier should be annotated
    entry = next(m for m in data if m["id"] == "gemini-2.0-flash")
    assert entry["synckey_tier"] == "mid"


def test_quality_floor_header_parsed(ctx):
    _add(ctx, n=1)
    ctx.router._index = {"claude-opus-4-8": {"groq"}}
    ctx.router._rebuild_tier_index()
    # Just verify the server doesn't crash when the header is present.
    with TestClient(create_app(ctx)) as client:
        r = client.post(
            "/v1/chat/completions",
            headers={**AUTH, "X-Quality-Floor": "high"},
            json={"model": "claude-opus-4-8", "messages": []},
        )
    # No real upstream; key is tried, connection refused -> 502. Should not 500.
    assert r.status_code in (200, 401, 404, 502, 503)


@respx.mock
def test_provider_field_stripped_from_upstream_body(ctx):
    import json as _json

    _add(ctx, n=1)
    route = respx.post(GROQ_CHAT).mock(
        return_value=httpx.Response(200, json={"choices": [], "usage": {}})
    )
    with TestClient(create_app(ctx)) as client:
        r = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "groq/llama-3.3-70b-versatile", "provider": "groq", "messages": []},
        )
    assert r.status_code == 200
    sent = _json.loads(route.calls.last.request.content)
    assert "provider" not in sent  # synckey-only hint must not leak upstream


@respx.mock
def test_default_model_used_when_model_omitted(ctx):
    _add(ctx, n=1)
    ctx.prefs.set_global_default("llama-3.3-70b-versatile")  # matches groq pattern
    ctx.router.reload_prefs()
    route = respx.post(GROQ_CHAT).mock(
        return_value=httpx.Response(200, json={"choices": [], "usage": {}})
    )
    with TestClient(create_app(ctx)) as client:
        r = client.post("/v1/chat/completions", headers=AUTH, json={"messages": []})
    assert r.status_code == 200
    assert route.call_count == 1


def test_no_model_no_default_returns_400(ctx):
    with TestClient(create_app(ctx)) as client:
        r = client.post("/v1/chat/completions", headers=AUTH, json={"messages": []})
    assert r.status_code == 400


def test_healthz_includes_key_counts(ctx):
    _add(ctx, n=2)
    ctx.pool.on_rate_limit(1, retry_after=60)
    ctx.pool.on_dead(2, "test")
    with TestClient(create_app(ctx)) as client:
        r = client.get("/healthz")
    body = r.json()
    assert "keys" in body
