"""The unified gateway: one OpenAI-compatible endpoint over every provider.

Endpoints:
    POST /v1/chat/completions   (streaming and non-streaming)
    POST /v1/embeddings
    POST /v1/completions
    GET  /v1/models             aggregated across configured providers
    GET  /v1/usage              live token totals
    GET  /healthz

Clients send the unified key as a bearer token. Each request is routed to a
provider, draws a live key from the pool, and on 429 or 5xx fails over to the
next key, then the next provider that serves the model. The hot path does no
blocking database work: key state is in memory and usage rows are handed to a
background writer.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .context import Context
from .db import sha256
from .keypool import parse_retry_after
from .providers import Provider
from .router import Resolution
from .usage import UsageWriter


@dataclass
class Attempt:
    provider_id: str
    provider: Provider
    key_id: int
    secret: str
    cooling: bool


def plan_attempts(ctx: Context, resolution: Resolution, max_retries: int) -> list[Attempt]:
    """Flatten resolved providers and their pooled keys into an ordered try-list."""
    attempts: list[Attempt] = []
    for pid in resolution.providers:
        prov = ctx.providers.get(pid)
        if not prov:
            continue
        for cand in ctx.pool.candidates(pid):
            attempts.append(Attempt(pid, prov, cand.key_id, cand.secret, cand.cooling))
    attempts.sort(key=lambda a: a.cooling)  # live keys first, throttled last
    return attempts[: max_retries + 1] if max_retries else attempts


def authorized(ctx: Context, request: Request) -> bool:
    expected = ctx.unified_key_hash()
    if not expected:
        return False
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header[:7].lower() == "bearer " else header.strip()
    return bool(token) and sha256(token) == expected


def unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": {"message": "Invalid unified key.", "type": "authentication_error"}},
        status_code=401,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx: Context = app.state.ctx
    settings = ctx.settings
    app.state.client = httpx.AsyncClient(
        timeout=settings.request_timeout,
        limits=httpx.Limits(
            max_connections=settings.max_connections,
            max_keepalive_connections=settings.max_keepalive,
        ),
    )
    app.state.writer = UsageWriter(ctx.db.path)
    app.state.writer.start()
    try:
        yield
    finally:
        await app.state.client.aclose()
        app.state.writer.stop()
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
        if not authorized(ctx, request):
            return unauthorized()
        data = [
            {"id": model, "object": "model", "owned_by": pid, "synckey_provider": pid}
            for model, providers in sorted(ctx.router.index.items())
            for pid in sorted(providers)
        ]
        return {"object": "list", "data": data}

    @app.get("/v1/usage")
    async def usage(request: Request):
        if not authorized(ctx, request):
            return unauthorized()
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
        return await forward(ctx, request, "/chat/completions")

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        return await forward(ctx, request, "/embeddings", allow_stream=False)

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await forward(ctx, request, "/completions")

    return app


def error_response(message: str, etype: str, status: int, **extra) -> JSONResponse:
    body = {"error": {"message": message, "type": etype, **extra}}
    return JSONResponse(body, status_code=status)


async def forward(
    ctx: Context, request: Request, path: str, allow_stream: bool = True
) -> Response:
    if not authorized(ctx, request):
        return unauthorized()

    try:
        body = await request.json()
    except Exception:
        return error_response("Body must be valid JSON.", "invalid_request_error", 400)

    model = body.get("model")
    if not model:
        return error_response("Field 'model' is required.", "invalid_request_error", 400)

    resolution = ctx.router.resolve(str(model))
    if not resolution.providers:
        return error_response(
            f"Could not route model '{model}'. No configured provider advertises it. "
            "Use a 'provider/model' prefix or run `synckey models --refresh`.",
            "not_found_error",
            404,
        )

    body["model"] = resolution.bare_model
    stream = bool(body.get("stream")) and allow_stream
    if stream:
        opts = body.get("stream_options") or {}
        opts["include_usage"] = True
        body["stream_options"] = opts

    attempts = plan_attempts(ctx, resolution, ctx.settings.max_retries)
    if not attempts:
        return error_response(
            f"Model '{model}' routes to {resolution.providers} but no keys are configured "
            "for those providers. Add one with `synckey key add`.",
            "configuration_error",
            503,
        )

    client: httpx.AsyncClient = request.app.state.client
    writer: UsageWriter = request.app.state.writer

    last_error = "unknown error"
    last_status = 502
    for attempt in attempts:
        url = attempt.provider.base_url.rstrip("/") + path
        headers = {"Content-Type": "application/json", **attempt.provider.auth_headers(attempt.secret)}
        params = attempt.provider.auth_params(attempt.secret)
        started = time.perf_counter()

        if stream:
            result = await open_stream(
                ctx, writer, client, url, headers, params, body, attempt, resolution, started
            )
            if result is not None:
                return result
            continue

        try:
            resp = await client.post(url, json=body, headers=headers, params=params)
        except httpx.HTTPError as exc:
            ctx.pool.report_failure(attempt.key_id)
            last_error, last_status = f"upstream error from {attempt.provider_id}: {exc}", 502
            continue

        latency = int((time.perf_counter() - started) * 1000)

        if resp.status_code == 200:
            ctx.pool.report_success(attempt.key_id)
            tokens = read_usage(resp.content)
            writer.record(
                provider=attempt.provider_id,
                model=resolution.bare_model,
                key_id=attempt.key_id,
                prompt_tokens=tokens[0],
                completion_tokens=tokens[1],
                total_tokens=tokens[2],
                status_code=200,
                latency_ms=latency,
            )
            # Pass the upstream bytes straight through, no re-serialization.
            return Response(content=resp.content, media_type="application/json")

        retriable, err_text = classify_failure(ctx, writer, attempt, resp, resolution, latency)
        last_error, last_status = err_text, resp.status_code
        if not retriable:
            return Response(
                content=resp.content, media_type="application/json", status_code=resp.status_code
            )

    return error_response(
        f"All {len(attempts)} key/provider attempts failed for '{model}'. Last: {last_error}",
        "upstream_exhausted",
        last_status if last_status >= 400 else 502,
        providers_tried=resolution.providers,
    )


def read_usage(content: bytes) -> tuple[int, int, int]:
    """Pull (prompt, completion, total) tokens from a JSON response body."""
    try:
        usage = json.loads(content).get("usage") or {}
    except (ValueError, AttributeError):
        return (0, 0, 0)
    return (
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        usage.get("total_tokens", 0),
    )


def classify_failure(
    ctx: Context,
    writer: UsageWriter,
    attempt: Attempt,
    resp: httpx.Response,
    resolution: Resolution,
    latency: int,
) -> tuple[bool, str]:
    """Decide whether a non-200 should be retried. Returns (retriable, message)."""
    text = resp.text[:300]
    writer.record(
        provider=attempt.provider_id,
        model=resolution.bare_model,
        key_id=attempt.key_id,
        status_code=resp.status_code,
        latency_ms=latency,
        error=text,
    )

    if resp.status_code == 429:
        ctx.pool.report_rate_limit(attempt.key_id, parse_retry_after(resp.headers.get("retry-after")))
        return True, text
    if resp.status_code >= 500:
        ctx.pool.report_failure(attempt.key_id)
        return True, text
    if resp.status_code in (401, 403):
        # Bad credential: cool it hard so the pool stops choosing it, keep trying others.
        ctx.pool.report_rate_limit(attempt.key_id, 300.0)
        return True, text
    # Other 4xx means the request itself is bad; do not burn more keys on it.
    return False, text


async def open_stream(
    ctx: Context,
    writer: UsageWriter,
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    params: dict,
    body: dict,
    attempt: Attempt,
    resolution: Resolution,
    started: float,
) -> StreamingResponse | None:
    """Open an upstream SSE stream. Returns a response on success, else None to
    try the next attempt."""
    req = client.build_request("POST", url, json=body, headers=headers, params=params)
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError:
        ctx.pool.report_failure(attempt.key_id)
        return None

    if resp.status_code != 200:
        latency = int((time.perf_counter() - started) * 1000)
        await resp.aread()
        classify_failure(ctx, writer, attempt, resp, resolution, latency)
        await resp.aclose()
        return None

    ctx.pool.report_success(attempt.key_id)

    async def stream_body() -> AsyncIterator[bytes]:
        prompt = completion = total = 0
        try:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    chunk = line[6:].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            obj = json.loads(chunk)
                            u = obj.get("usage")
                            if isinstance(u, dict):
                                prompt = u.get("prompt_tokens", prompt)
                                completion = u.get("completion_tokens", completion)
                                total = u.get("total_tokens", total)
                        except ValueError:
                            pass
                yield (line + "\n").encode("utf-8")
        finally:
            await resp.aclose()
            writer.record(
                provider=attempt.provider_id,
                model=resolution.bare_model,
                key_id=attempt.key_id,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                status_code=200,
                latency_ms=int((time.perf_counter() - started) * 1000),
                stream=True,
            )

    return StreamingResponse(stream_body(), media_type="text/event-stream")
