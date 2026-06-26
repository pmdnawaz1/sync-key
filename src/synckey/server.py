"""The unified gateway.

Endpoints:
    POST /v1/chat/completions   streaming and non-streaming
    POST /v1/embeddings
    POST /v1/completions
    GET  /v1/models             aggregated across configured providers
    GET  /v1/usage              live token totals
    GET  /v1/events             recent routing events
    GET  /healthz

Request header X-Quality-Floor: frontier|high|mid|low overrides the automatic
tier floor for a single request. Without it, the floor equals the requested
model's own tier (Opus stays Opus-class; gpt-4o-mini can fall to other mid
models if needed).
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
import orjson
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .context import Context
from .db import sha256
from .keypool import KeyPool, parse_retry_after, Candidate
from .providers import Provider
from .router import Resolution
from .tiers import TIER_BY_NAME, cost_usd, model_tier
from .usage import UsageWriter


@dataclass
class Attempt:
    provider_id: str
    provider: Provider
    key_id: int
    secret: str
    candidate: Candidate


def plan_attempts(ctx: Context, resolution: Resolution, max_retries: int) -> list[Attempt]:
    attempts: list[Attempt] = []
    for pid in resolution.providers:
        prov = ctx.providers.get(pid)
        if not prov:
            continue
        for cand in ctx.pool.candidates(pid):
            attempts.append(Attempt(pid, prov, cand.key_id, cand.secret, cand))
    # ready (bucket has space) before throttled before cooling
    attempts.sort(key=lambda a: (a.candidate.cooling, not a.candidate.ready))
    return attempts[: max_retries + 1]


def all_deep_cooling(attempts: list[Attempt], threshold: float) -> bool:
    """True when every candidate is cooling and won't free up for a while."""
    return bool(attempts) and all(
        a.candidate.cooling and a.candidate.cooldown_remaining >= threshold
        for a in attempts
    )


def build_tier_alt_attempts(ctx: Context, resolution: Resolution, body: dict) -> list[Attempt]:
    """Find ready candidates on same-tier alternative models."""
    if not (resolution.tier and resolution.floor):
        return []
    alts = ctx.router.tier_alternatives(resolution.tier, resolution.floor, resolution.bare_model)
    for alt_model, alt_pid in alts:
        prov = ctx.providers.get(alt_pid)
        if not prov:
            continue
        # Only take candidates that are not themselves cooling.
        cands = [c for c in ctx.pool.candidates(alt_pid) if not c.cooling]
        if not cands:
            continue
        ctx.db.record_event(
            type="tier_fallback",
            provider=alt_pid,
            model=resolution.bare_model,
            fallback_to=alt_model,
            tier_from=resolution.tier,
            tier_to=resolution.tier,
            message=f"Primary blocked; routing to {alt_model} on {alt_pid}",
        )
        body["model"] = alt_model
        return [Attempt(alt_pid, prov, c.key_id, c.secret, c) for c in cands]
    return []


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


def err(message: str, etype: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": etype, **extra}}, status_code=status)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx: Context = app.state.ctx
    s = ctx.settings
    app.state.client = httpx.AsyncClient(
        timeout=s.request_timeout,
        limits=httpx.Limits(
            max_connections=s.max_connections,
            max_keepalive_connections=s.max_keepalive,
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
        states = ctx.states.all()
        from .state import Health
        dead = sum(1 for s in states.values() if s.health == Health.DEAD)
        cooling = sum(1 for s in states.values() if s.health == Health.COOLING and s.cooldown_remaining() > 0)
        live = len(states) - dead - cooling
        return {
            "status": "ok",
            "providers": ctx.db.providers_with_keys(),
            "models_indexed": len(ctx.router.index),
            "keys": {"live": live, "cooling": cooling, "dead": dead},
        }

    @app.get("/v1/models")
    async def list_models(request: Request):
        if not authorized(ctx, request):
            return unauthorized()
        from .tiers import model_tier, TIER_NAMES
        data = [
            {
                "id": model,
                "object": "model",
                "owned_by": pid,
                "synckey_provider": pid,
                "synckey_tier": TIER_NAMES.get(model_tier(model) or 0, "unknown"),
            }
            for model, providers in sorted(ctx.router.index.items())
            for pid in sorted(providers)
        ]
        return Response(content=orjson.dumps({"object": "list", "data": data}), media_type="application/json")

    @app.get("/v1/usage")
    async def usage_summary(request: Request):
        if not authorized(ctx, request):
            return unauthorized()
        totals = ctx.db.usage_totals()
        return {
            "requests": totals["requests"] or 0,
            "total_tokens": totals["total_tokens"] or 0,
            "prompt_tokens": totals["prompt_tokens"] or 0,
            "completion_tokens": totals["completion_tokens"] or 0,
            "cost_usd": round(totals["cost_usd"] or 0.0, 6),
            "errors": totals["errors"] or 0,
        }

    @app.get("/v1/events")
    async def events(request: Request, limit: int = 50):
        if not authorized(ctx, request):
            return unauthorized()
        rows = ctx.db.recent_events(limit)
        return {
            "events": [
                {
                    "ts": r["ts"],
                    "type": r["type"],
                    "provider": r["provider"],
                    "model": r["model"],
                    "fallback_to": r["fallback_to"],
                    "tier_from": r["tier_from"],
                    "tier_to": r["tier_to"],
                    "message": r["message"],
                }
                for r in rows
            ]
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


async def forward(
    ctx: Context, request: Request, path: str, allow_stream: bool = True
) -> Response:
    if not authorized(ctx, request):
        return unauthorized()

    try:
        body = await request.json()
    except Exception:
        return err("Body must be valid JSON.", "invalid_request_error", 400)

    model = body.get("model")
    if not model:
        return err("Field 'model' is required.", "invalid_request_error", 400)

    # Parse optional quality floor from caller.
    floor_header = request.headers.get("x-quality-floor")
    floor = TIER_BY_NAME.get(floor_header.lower()) if floor_header else None

    resolution = ctx.router.resolve(str(model), floor=floor)
    if not resolution.providers:
        return err(
            f"Could not route '{model}'. No provider advertises it. "
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

    client: httpx.AsyncClient = request.app.state.client
    writer: UsageWriter = request.app.state.writer

    attempts = plan_attempts(ctx, resolution, ctx.settings.max_retries)

    # Jump to same-tier alternatives when primary is gone or all keys deep-cooling.
    if ctx.settings.tier_fallback_enabled:
        threshold = ctx.settings.deep_cooling_threshold
        if not attempts or all_deep_cooling(attempts, threshold):
            alt_attempts = build_tier_alt_attempts(ctx, resolution, body)
            if alt_attempts:
                attempts = alt_attempts
            # If no tier alts found, fall through to the cooling primaries (beats 503).

    if not attempts:
        return err(
            f"'{model}' has no available keys. All keys may be cooling, dead, or exhausted. "
            "Run `synckey key list` to check.",
            "no_available_keys",
            503,
        )

    last_err = "unknown"
    last_status = 502
    for attempt in attempts:
        url = attempt.provider.base_url.rstrip("/") + path
        headers = {
            "Content-Type": "application/json",
            **attempt.provider.auth_headers(attempt.secret),
        }
        params = attempt.provider.auth_params(attempt.secret)
        ctx.pool.consume(attempt.key_id)
        started = time.perf_counter()

        if stream:
            result = await try_stream(ctx, writer, client, url, headers, params, body, attempt, resolution, started)
            if result is not None:
                return result
            continue

        try:
            resp = await client.post(url, json=body, headers=headers, params=params)
        except httpx.HTTPError as exc:
            ctx.pool.on_failure(attempt.key_id)
            last_err, last_status = str(exc)[:200], 502
            continue

        latency = int((time.perf_counter() - started) * 1000)

        if resp.status_code == 200:
            ctx.pool.on_success(attempt.key_id)
            tokens = extract_usage(resp.content)
            writer.record(
                provider=attempt.provider_id,
                model=body["model"],
                key_id=attempt.key_id,
                prompt_tokens=tokens[0],
                completion_tokens=tokens[1],
                total_tokens=tokens[2],
                cost_usd=cost_usd(body["model"], tokens[0], tokens[1]),
                status_code=200,
                latency_ms=latency,
                tier=resolution.tier,
            )
            return Response(content=resp.content, media_type="application/json")

        retriable, last_err, last_status = classify_failure(
            ctx, writer, attempt, resp, resolution, body["model"], latency
        )
        if not retriable:
            return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)

    return err(
        f"All {len(attempts)} attempts failed for '{model}'. Last: {last_err}",
        "upstream_exhausted",
        last_status if last_status >= 400 else 502,
        providers_tried=resolution.providers,
    )


def extract_usage(content: bytes) -> tuple[int, int, int]:
    try:
        u = orjson.loads(content).get("usage") or {}
        return u.get("prompt_tokens", 0), u.get("completion_tokens", 0), u.get("total_tokens", 0)
    except Exception:
        return 0, 0, 0


def classify_failure(
    ctx: Context,
    writer: UsageWriter,
    attempt: Attempt,
    resp: httpx.Response,
    resolution: Resolution,
    model: str,
    latency: int,
) -> tuple[bool, str, int]:
    text = resp.text[:300]
    writer.record(
        provider=attempt.provider_id,
        model=model,
        key_id=attempt.key_id,
        status_code=resp.status_code,
        latency_ms=latency,
        tier=resolution.tier,
        error=text,
    )

    if resp.status_code == 429:
        retry_after = parse_retry_after(resp.headers.get("retry-after"))
        rpm_at_429 = attempt.candidate.bucket.emission_rpm()
        ctx.pool.on_rate_limit(attempt.key_id, retry_after, rpm_at_429)
        ctx.db.record_event(
            type="rate_limited",
            key_id=attempt.key_id,
            provider=attempt.provider_id,
            model=model,
            message=f"429 from {attempt.provider_id}; cooldown {retry_after or 'default'}s",
        )
        return True, text, 429

    if resp.status_code in (401, 403):
        reason = f"HTTP {resp.status_code} from {attempt.provider_id}: key likely expired or invalid"
        ctx.pool.on_dead(attempt.key_id, reason)
        ctx.db.record_event(
            type="key_dead",
            key_id=attempt.key_id,
            provider=attempt.provider_id,
            model=model,
            message=reason,
        )
        return True, text, resp.status_code

    if resp.status_code >= 500:
        ctx.pool.on_failure(attempt.key_id)
        return True, text, resp.status_code

    # 4xx other than 401/403/429: the request itself is wrong; don't burn more keys.
    return False, text, resp.status_code


async def try_stream(
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
    req = client.build_request("POST", url, json=body, headers=headers, params=params)
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError:
        ctx.pool.on_failure(attempt.key_id)
        return None

    if resp.status_code != 200:
        latency = int((time.perf_counter() - started) * 1000)
        await resp.aread()
        classify_failure(ctx, writer, attempt, resp, resolution, body["model"], latency)
        await resp.aclose()
        return None

    ctx.pool.on_success(attempt.key_id)

    async def stream_body() -> AsyncIterator[bytes]:
        prompt = completion = total = 0
        try:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    chunk = line[6:].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            u = orjson.loads(chunk).get("usage")
                            if isinstance(u, dict):
                                prompt = u.get("prompt_tokens", prompt)
                                completion = u.get("completion_tokens", completion)
                                total = u.get("total_tokens", total)
                        except Exception:
                            pass
                yield (line + "\n").encode()
        finally:
            await resp.aclose()
            writer.record(
                provider=attempt.provider_id,
                model=body["model"],
                key_id=attempt.key_id,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                cost_usd=cost_usd(body["model"], prompt, completion),
                status_code=200,
                latency_ms=int((time.perf_counter() - started) * 1000),
                stream=True,
                tier=resolution.tier,
            )

    return StreamingResponse(stream_body(), media_type="text/event-stream")
