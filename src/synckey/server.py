"""The unified gateway: one OpenAI-compatible endpoint, every provider behind it.

Exposes:
    POST /v1/chat/completions   (streaming + non-streaming)
    POST /v1/embeddings
    GET  /v1/models             (aggregated across configured providers)
    GET  /v1/usage              (synckey extension: live token totals)
    GET  /healthz

Auth: clients send the unified key as ``Authorization: Bearer sk-synckey-...``.
Internally the request is routed to a provider, a live key is drawn from the
pool, and on 429/5xx the request fails over to the next key/provider — the
"rate-limit eater" — so callers just keep getting answers.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .context import Context
from .db import sha256
from .keypool import parse_retry_after
from .providers import Provider
from .router import Resolution


@dataclass
class Attempt:
    provider_id: str
    provider: Provider
    key_id: int
    label: str
    secret: str
    cooling: bool


def _build_plan(ctx: Context, resolution: Resolution, max_retries: int) -> list[Attempt]:
    """Flatten resolved providers + their pooled keys into an ordered try-list."""
    plan: list[Attempt] = []
    for pid in resolution.providers:
        prov = ctx.providers.get(pid)
        if not prov:
            continue
        for cand in ctx.pool.candidates(pid):
            plan.append(
                Attempt(pid, prov, cand.key_id, cand.label, cand.secret, cand.cooling)
            )
    # Live keys first across all providers, throttled keys last; stable within.
    plan.sort(key=lambda a: a.cooling)
    return plan[: max_retries + 1] if max_retries else plan


def _auth_ok(ctx: Context, request: Request) -> bool:
    expected = ctx.unified_key_hash()
    if not expected:
        return False
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else header.strip()
    return bool(token) and sha256(token) == expected


def _unauth() -> JSONResponse:
    return JSONResponse(
        {"error": {"message": "Invalid unified key.", "type": "authentication_error"}},
        status_code=401,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx: Context = app.state.ctx
    app.state.client = httpx.AsyncClient(timeout=ctx.settings.request_timeout)
    try:
        yield
    finally:
        await app.state.client.aclose()
        # Only tear down a context this app created; a caller-supplied ctx
        # (CLI, tests) owns its own lifecycle.
        if app.state.owns_ctx:
            ctx.close()


def create_app(ctx: Context | None = None) -> FastAPI:
    owns_ctx = ctx is None
    ctx = ctx or Context()
    app = FastAPI(title="synckey gateway", version="0.1.0", lifespan=lifespan)
    app.state.ctx = ctx
    app.state.owns_ctx = owns_ctx

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "providers_configured": ctx.db.providers_with_keys(),
            "models_indexed": len(ctx.router.index),
        }

    @app.get("/v1/models")
    async def list_models(request: Request):
        if not _auth_ok(ctx, request):
            return _unauth()
        data = []
        for model, providers in sorted(ctx.router.index.items()):
            for pid in sorted(providers):
                data.append(
                    {"id": model, "object": "model", "owned_by": pid, "synckey_provider": pid}
                )
        return {"object": "list", "data": data}

    @app.get("/v1/usage")
    async def usage(request: Request):
        if not _auth_ok(ctx, request):
            return _unauth()
        totals = ctx.db.usage_totals()
        return {
            "requests": totals["requests"] or 0,
            "total_tokens": totals["total_tokens"] or 0,
            "prompt_tokens": totals["prompt_tokens"] or 0,
            "completion_tokens": totals["completion_tokens"] or 0,
            "errors": totals["errors"] or 0,
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await _proxy(ctx, app.state.client, request, "/chat/completions")

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        return await _proxy(ctx, app.state.client, request, "/embeddings", allow_stream=False)

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await _proxy(ctx, app.state.client, request, "/completions")

    return app


async def _proxy(
    ctx: Context,
    client: httpx.AsyncClient,
    request: Request,
    path: str,
    allow_stream: bool = True,
) -> JSONResponse | StreamingResponse:
    if not _auth_ok(ctx, request):
        return _unauth()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": {"message": "Body must be valid JSON.", "type": "invalid_request_error"}},
            status_code=400,
        )

    model = body.get("model")
    if not model:
        return JSONResponse(
            {"error": {"message": "Field 'model' is required.", "type": "invalid_request_error"}},
            status_code=400,
        )

    resolution = ctx.router.resolve(str(model))
    if not resolution.providers:
        return JSONResponse(
            {
                "error": {
                    "message": (
                        f"Could not route model '{model}'. No configured provider advertises it. "
                        "Try a 'provider/model' prefix, or run `synckey models --refresh`."
                    ),
                    "type": "not_found_error",
                }
            },
            status_code=404,
        )

    body["model"] = resolution.bare_model
    stream = bool(body.get("stream")) and allow_stream
    if stream:
        # Ask upstream to emit a final usage chunk so we can meter streams.
        opts = body.get("stream_options") or {}
        opts["include_usage"] = True
        body["stream_options"] = opts

    plan = _build_plan(ctx, resolution, ctx.settings.max_retries)
    if not plan:
        return JSONResponse(
            {
                "error": {
                    "message": (
                        f"Model '{model}' routes to {resolution.providers} but no keys are "
                        "configured for those providers. Add one with `synckey key add`."
                    ),
                    "type": "configuration_error",
                }
            },
            status_code=503,
        )

    last_error: dict | None = None
    last_status = 502
    for attempt in plan:
        url = attempt.provider.base_url.rstrip("/") + path
        headers = {"Content-Type": "application/json"}
        headers.update(attempt.provider.auth_headers(attempt.secret))
        params = attempt.provider.auth_params(attempt.secret)
        ctx.db.mark_attempt(attempt.key_id)
        started = time.perf_counter()

        if stream:
            result = await _try_stream(
                ctx, client, url, headers, params, body, attempt, resolution, started
            )
            if result is not None:
                return result
            continue

        try:
            resp = await client.post(url, json=body, headers=headers, params=params)
        except httpx.HTTPError as exc:
            ctx.pool.report_failure(attempt.key_id)
            last_error = {"message": f"Upstream error from {attempt.provider_id}: {exc}"}
            last_status = 502
            continue

        latency = int((time.perf_counter() - started) * 1000)

        if resp.status_code == 200:
            payload = resp.json()
            usage = payload.get("usage") or {}
            ctx.pool.report_success(attempt.key_id)
            ctx.db.record_usage(
                provider=attempt.provider_id,
                model=resolution.bare_model,
                key_id=attempt.key_id,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                status_code=200,
                latency_ms=latency,
            )
            return JSONResponse(payload, status_code=200)

        # Non-200: decide whether to eat-and-retry or surface.
        retriable, err_body = _handle_failure(ctx, attempt, resp, resolution, latency)
        last_error, last_status = err_body, resp.status_code
        if not retriable:
            return JSONResponse(err_body, status_code=resp.status_code)
        # else fall through to next attempt

    return JSONResponse(
        {
            "error": {
                "message": (
                    f"All {len(plan)} key/provider attempts failed for '{model}'. "
                    f"Last: {(last_error or {}).get('message', 'unknown error')}"
                ),
                "type": "upstream_exhausted",
                "providers_tried": resolution.providers,
            }
        },
        status_code=last_status if last_status >= 400 else 502,
    )


def _handle_failure(
    ctx: Context, attempt: Attempt, resp: httpx.Response, resolution: Resolution, latency: int
) -> tuple[bool, dict]:
    """Classify a non-200 response. Returns (retriable, error_body)."""
    try:
        err_body = resp.json()
    except Exception:
        err_body = {"error": {"message": resp.text[:500] or "upstream error"}}

    ctx.db.record_usage(
        provider=attempt.provider_id,
        model=resolution.bare_model,
        key_id=attempt.key_id,
        status_code=resp.status_code,
        latency_ms=latency,
        error=json.dumps(err_body)[:500],
    )

    if resp.status_code == 429:
        retry_after = parse_retry_after(resp.headers.get("retry-after"))
        ctx.pool.report_rate_limit(attempt.key_id, retry_after)
        return True, err_body
    if resp.status_code >= 500:
        ctx.pool.report_failure(attempt.key_id)
        return True, err_body
    if resp.status_code in (401, 403):
        # Bad credential: cool it hard so the pool stops picking it, but keep
        # trying other keys/providers.
        ctx.pool.report_rate_limit(attempt.key_id, 300.0)
        return True, err_body
    # Other 4xx (e.g. malformed request): the request itself is the problem.
    return False, err_body


async def _try_stream(
    ctx: Context,
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    params: dict,
    body: dict,
    attempt: Attempt,
    resolution: Resolution,
    started: float,
) -> StreamingResponse | None:
    """Open an upstream stream. Returns a StreamingResponse on success, or
    ``None`` to signal the caller to try the next attempt."""
    req = client.build_request("POST", url, json=body, headers=headers, params=params)
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError:
        ctx.pool.report_failure(attempt.key_id)
        return None

    if resp.status_code != 200:
        latency = int((time.perf_counter() - started) * 1000)
        await resp.aread()
        _handle_failure(ctx, attempt, resp, resolution, latency)
        await resp.aclose()
        return None

    ctx.pool.report_success(attempt.key_id)

    async def gen() -> AsyncIterator[bytes]:
        prompt_t = completion_t = total_t = 0
        try:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    chunk = line[6:].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            obj = json.loads(chunk)
                            if isinstance(obj.get("usage"), dict):
                                u = obj["usage"]
                                prompt_t = u.get("prompt_tokens", prompt_t)
                                completion_t = u.get("completion_tokens", completion_t)
                                total_t = u.get("total_tokens", total_t)
                        except ValueError:
                            pass
                yield (line + "\n").encode("utf-8")
                if not line:
                    continue
        finally:
            await resp.aclose()
            latency = int((time.perf_counter() - started) * 1000)
            ctx.db.record_usage(
                provider=attempt.provider_id,
                model=resolution.bare_model,
                key_id=attempt.key_id,
                prompt_tokens=prompt_t,
                completion_tokens=completion_t,
                total_tokens=total_t,
                status_code=200,
                latency_ms=latency,
                stream=True,
            )

    return StreamingResponse(gen(), media_type="text/event-stream")
