"""Deferred request queue helpers.

When every key for a request is cooling, the gateway can't serve it now. Instead
of a 503, it persists the request, hands back an id + ETA, and a background worker
replays it through the normal keypool once capacity frees. The result is stored
and served on a later GET, then purged `deferred_ttl` seconds after completion.

This module holds the small pure helpers; the worker loop and the run path live
in server.py (they reuse its forwarding logic).
"""

from __future__ import annotations

import secrets

DEFER_PREFIX = "defer_"


def new_request_id() -> str:
    return DEFER_PREFIX + secrets.token_urlsafe(18)


def estimate_eta(ctx, providers: list[str]) -> float:
    """Seconds until the soonest key for these providers is expected to be usable.

    Accounts for both reasons a key can be unavailable: a provider cooldown (use
    its remaining window) and a drained local bucket (use its refill time). Falls
    back to the configured default cooldown when nothing reports a wait.
    """
    waits: list[float] = []
    for pid in providers:
        for cand in ctx.pool.candidates(pid):
            if cand.ready:
                return 0.0  # something can already go (rare at defer time)
            if cand.cooling and cand.cooldown_remaining > 0:
                waits.append(cand.cooldown_remaining)
            else:
                waits.append(cand.bucket.seconds_to_capacity())
    if waits:
        return max(0.0, min(waits))
    return float(ctx.settings.default_cooldown)
