import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from synckey.server import create_app

AUTH = {"Authorization": "Bearer sk-synckey-test"}
GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"


def _seed_keys(ctx, provider="groq", n=2):
    return [ctx.db.add_key(provider, f"k{i}", ctx.box.seal(f"secret-{i}")) for i in range(n)]


def test_auth_required(ctx):
    with TestClient(create_app(ctx)) as client:
        r = client.post("/v1/chat/completions", json={"model": "groq/x"})
        assert r.status_code == 401


def test_unroutable_model_404(ctx):
    with TestClient(create_app(ctx)) as client:
        r = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "no-such-model-anywhere"},
        )
        assert r.status_code == 404


@respx.mock
def test_happy_path_records_usage(ctx):
    _seed_keys(ctx)
    respx.post(GROQ_CHAT).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
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
    totals = ctx.db.usage_totals()
    assert totals["requests"] == 1
    assert totals["total_tokens"] == 12


@respx.mock
def test_rate_limit_failover_eats_429(ctx):
    _seed_keys(ctx, n=2)
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
    # The 429 was eaten; the second key succeeded.
    assert r.status_code == 200
    assert route.call_count == 2


@respx.mock
def test_client_error_not_retried(ctx):
    _seed_keys(ctx, n=2)
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
    assert route.call_count == 1  # a 400 is the caller's fault; don't burn keys


def test_models_endpoint_lists_index(ctx):
    ctx.router._index = {"gemini-2.0-flash": {"gemini"}}
    with TestClient(create_app(ctx)) as client:
        r = client.get("/v1/models", headers=AUTH)
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert "gemini-2.0-flash" in ids
