"""Deferred queue: 202 when all keys cool, GET retrieval, worker execution, purge."""

import asyncio
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from synckey.server import _run_deferred_job, create_app

AUTH = {"Authorization": "Bearer sk-synckey-test"}
GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "groq/llama-3.3-70b-versatile"


def _add(ctx, n=1):
    return [ctx.db.add_key("groq", f"k{i}", ctx.box.seal(f"secret-{i}")) for i in range(n)]


def _cool_all(ctx, ids):
    for kid in ids:
        ctx.pool.on_rate_limit(kid, retry_after=120)


def test_all_cooling_returns_202_and_queues(ctx):
    ids = _add(ctx, n=1)
    _cool_all(ctx, ids)
    with TestClient(create_app(ctx)) as client:
        r = client.post("/v1/chat/completions", headers=AUTH, json={"model": MODEL, "messages": []})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["id"].startswith("defer_")
    assert body["retry_after"] >= 1
    assert "Retry-After" in r.headers
    assert ctx.db.count_deferred("queued") == 1


def test_all_locally_throttled_defers(ctx):
    # A LIVE key (not cooling) whose local bucket is drained should also defer.
    kid = ctx.db.add_key("groq", "k", ctx.box.seal("s"), rpm_limit=1)
    ctx.pool.candidates("groq")  # instantiate the bucket with rpm_cap=1
    ctx.pool.consume(kid)        # drain its single token -> throttled, not cooling
    from synckey.state import Health
    assert ctx.states.get(kid).health == Health.LIVE
    with TestClient(create_app(ctx)) as client:
        r = client.post("/v1/chat/completions", headers=AUTH, json={"model": MODEL, "messages": []})
    assert r.status_code == 202
    assert ctx.db.count_deferred("queued") == 1


@respx.mock
def test_deferred_disabled_does_not_queue(ctx):
    # With deferral off, an all-cooling request is NOT queued; the cooling key is
    # tried as a last resort (here the upstream is still rate-limiting).
    ids = _add(ctx, n=1)
    _cool_all(ctx, ids)
    ctx.settings.deferred_enabled = False
    respx.post(GROQ_CHAT).mock(return_value=httpx.Response(429, json={"error": "slow down"}))
    with TestClient(create_app(ctx)) as client:
        r = client.post("/v1/chat/completions", headers=AUTH, json={"model": MODEL, "messages": []})
    assert r.status_code != 202
    assert ctx.db.count_deferred() == 0


def test_get_unknown_id_404(ctx):
    with TestClient(create_app(ctx)) as client:
        r = client.get("/v1/requests/defer_nope", headers=AUTH)
    assert r.status_code == 404


def test_get_pending_returns_202(ctx):
    ids = _add(ctx, n=1)
    _cool_all(ctx, ids)
    with TestClient(create_app(ctx)) as client:
        rid = client.post(
            "/v1/chat/completions", headers=AUTH, json={"model": MODEL, "messages": []}
        ).json()["id"]
        r = client.get(f"/v1/requests/{rid}", headers=AUTH)
    assert r.status_code == 202
    assert r.json()["status"] in ("queued", "running")


def test_get_requires_auth(ctx):
    with TestClient(create_app(ctx)) as client:
        r = client.get("/v1/requests/defer_x")
    assert r.status_code == 401


@respx.mock
def test_worker_runs_job_and_get_serves_response(ctx):
    ids = _add(ctx, n=1)
    _cool_all(ctx, ids)
    # Queue a job via the API.
    with TestClient(create_app(ctx)) as client:
        rid = client.post(
            "/v1/chat/completions", headers=AUTH, json={"model": MODEL, "messages": []}
        ).json()["id"]

        # Keys fully recover: clear the cooldown and reset the drained bucket.
        ctx.states.set_live(ids[0])
        ctx.pool._buckets.drop(ids[0])
        respx.post(GROQ_CHAT).mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "done"}}], "usage": {"total_tokens": 3}},
            )
        )
        row = ctx.db.get_deferred(rid)

        class _W:  # minimal usage writer stub
            def record(self, **k):
                pass

        async def _drive():
            async with httpx.AsyncClient() as hc:
                await _run_deferred_job(ctx, hc, _W(), row)

        asyncio.run(_drive())

        done = ctx.db.get_deferred(rid)
        assert done["status"] == "done"
        assert done["status_code"] == 200

        r = client.get(f"/v1/requests/{rid}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "done"


def test_purge_removes_completed_after_ttl(ctx):
    ctx.db.create_deferred(
        id="defer_old", path="/chat/completions", body="{}", model="x",
        provider_hint=None, floor=None, eta=time.time(),
    )
    ctx.db.complete_deferred("defer_old", 200, b"{}")
    # Backdate completion well beyond the TTL.
    ctx.db.conn.execute(
        "UPDATE deferred SET completed_at=? WHERE id=?", (time.time() - 10000, "defer_old")
    )
    ctx.db.conn.commit()
    removed = ctx.db.purge_deferred(ttl=3600, max_queue_age=86400)
    assert removed == 1
    assert ctx.db.get_deferred("defer_old") is None
