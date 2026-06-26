"""synckey command-line interface.

    synckey init                  one-time setup; prints your unified key
    synckey providers             list every supported provider
    synckey key add <provider>    store a credential (--key supports comma-separated bulk)
    synckey key import <provider> bulk import from --file or --keys
    synckey key list              key health matrix with burn rate
    synckey key rm|enable|disable
    synckey key limits <id>       set RPM/TPM limits for a key
    synckey models [--refresh]    discover models your keys can call
    synckey detect <model>        show routing and tier
    synckey serve                 run the unified gateway
    synckey dash                  live dashboard (keys, events, burn rate)
    synckey usage [--recent]      token and cost monitoring
    synckey spend                 cost breakdown by model
    synckey events [--type]       routing events log
    synckey status                at-a-glance overview
    synckey test [provider]       health-check stored keys
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import ensure_home, generate_unified_key, home
from .context import Context
from .db import sha256
from .state import Health
from .tiers import TIER_NAMES, model_tier, model_price

app = typer.Typer(
    name="synckey",
    help="Merge every AI provider key behind one unified, OpenAI-compatible API key.",
    no_args_is_help=True,
    add_completion=False,
)
key_app = typer.Typer(help="Manage stored provider credentials.", no_args_is_help=True)
app.add_typer(key_app, name="key")

console = Console()
err_con = Console(stderr=True)


def load_ctx() -> Context:
    ensure_home()
    return Context()


def fmt_num(n: int | None) -> str:
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_cost(usd: float | None) -> str:
    if usd is None:
        return ""
    if usd < 0.001:
        return f"${usd * 1000:.3f}m"  # milli-dollars
    return f"${usd:.4f}"


def health_text(h: Health, remaining: float = 0) -> Text:
    if h == Health.DEAD:
        return Text("DEAD", style="bold red")
    if h == Health.COOLING:
        return Text(f"COOLING {int(remaining)}s", style="yellow")
    return Text("LIVE", style="green")


@app.command()
def init(force: bool = typer.Option(False, "--force", help="Regenerate the unified key.")):
    """Initialize synckey and mint your unified API key."""
    ensure_home()
    ctx = Context()
    if ctx.unified_key_hash() and not force:
        err_con.print("[yellow]Already initialized.[/] Use --force to mint a new key.")
        console.print(f"Config home: [cyan]{home()}[/]")
        raise typer.Exit(0)

    unified = generate_unified_key()
    ctx.db.set_meta("unified_key_hash", sha256(unified))
    ctx.db.set_meta("created_at", str(time.time()))
    _ = ctx.box

    console.print(
        Panel.fit(
            f"[bold green]synckey is ready[/]\n\n"
            f"Your unified API key (shown once, store it now):\n\n"
            f"  [bold cyan]{unified}[/]\n\n"
            f"Point any OpenAI-compatible client at the gateway:\n"
            f"  base_url = http://{ctx.settings.host}:{ctx.settings.port}/v1\n"
            f"  api_key  = the key above\n\n"
            f"Next: [bold]synckey key add groq[/]  then  [bold]synckey serve[/]",
            title="Unified Key",
            border_style="green",
        )
    )
    ctx.close()


@app.command()
def providers():
    """List every supported provider and where to get a key."""
    ctx = load_ctx()
    configured = set(ctx.db.providers_with_keys())
    table = Table(title="Supported providers")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("name")
    table.add_column("keys", justify="right")
    table.add_column("auth", style="dim")
    table.add_column("get a key", style="blue")
    for pid, prov in sorted(ctx.providers.items()):
        n = len(ctx.db.list_keys(provider=pid))
        flag = f"[green]{n}[/]" if pid in configured else str(n)
        table.add_row(pid, prov.name, flag, prov.auth, prov.signup or prov.notes or "")
    console.print(table)
    ctx.close()


def _add_one_key(ctx, provider: str, secret: str, label: str | None, weight: int, rpm, tpm) -> int:
    existing = len(ctx.db.list_keys(provider=provider))
    lbl = label or f"{provider}-{existing + 1}"
    key_id = ctx.db.add_key(provider, lbl, ctx.box.seal(secret), weight=weight, rpm_limit=rpm, tpm_limit=tpm)
    masked = secret[-4:] if len(secret) >= 4 else "***"
    limits = (f" RPM={rpm}" if rpm else "") + (f" TPM={fmt_num(int(tpm))}" if tpm else "")
    console.print(
        f"[green]Added[/] {ctx.providers[provider].name} key [cyan]#{key_id}[/] "
        f"(label={lbl}, weight={weight}{limits}). Masked: ...{masked}"
    )
    return key_id


@key_app.command("add")
def key_add(
    provider: str = typer.Argument(..., help="Provider id, e.g. groq, gemini, cohere."),
    key: str = typer.Option(None, "--key", "-k", help="Secret key. Comma-separate for bulk."),
    label: str = typer.Option(None, "--label", "-l"),
    weight: int = typer.Option(1, "--weight", "-w"),
    from_env: bool = typer.Option(False, "--from-env"),
    rpm: float = typer.Option(None, "--rpm", help="Known RPM limit for this key."),
    tpm: float = typer.Option(None, "--tpm", help="Known TPM limit for this key."),
):
    """Store a credential. Comma-separate multiple keys for bulk add."""
    ctx = load_ctx()
    provider = provider.lower()
    if provider not in ctx.providers:
        err_con.print(f"[red]Unknown provider '{provider}'.[/] Run `synckey providers`.")
        raise typer.Exit(1)

    prov = ctx.providers[provider]
    secret = key
    if not secret and from_env:
        for env in prov.env:
            if os.environ.get(env):
                secret = os.environ[env]
                console.print(f"[dim]Read key from ${env}[/]")
                break
        if not secret:
            err_con.print(f"[red]No key in env vars:[/] {', '.join(prov.env) or '(none)'}")
            raise typer.Exit(1)
    if not secret:
        secret = typer.prompt(f"{prov.name} API key", hide_input=True)
    secret = secret.strip()
    if not secret:
        err_con.print("[red]Empty key.[/]")
        raise typer.Exit(1)

    secrets = [s.strip() for s in secret.split(",") if s.strip()]
    for i, s in enumerate(secrets):
        lbl = label if len(secrets) == 1 else (f"{label}-{i+1}" if label else None)
        _add_one_key(ctx, provider, s, lbl, weight, rpm, tpm)
    ctx.close()


@key_app.command("import")
def key_import(
    provider: str = typer.Argument(..., help="Provider id, e.g. groq, gemini."),
    file: Path = typer.Option(None, "--file", "-f", help="File with one key per line."),
    keys: str = typer.Option(None, "--keys", "-k", help="Comma-separated keys."),
    from_env: bool = typer.Option(False, "--from-env", help="Import all matching env vars."),
    weight: int = typer.Option(1, "--weight", "-w"),
    rpm: float = typer.Option(None, "--rpm"),
    tpm: float = typer.Option(None, "--tpm"),
):
    """Bulk-import keys: --file keys.txt, --keys k1,k2,k3, or --from-env."""
    ctx = load_ctx()
    provider = provider.lower()
    if provider not in ctx.providers:
        err_con.print(f"[red]Unknown provider '{provider}'.[/] Run `synckey providers`.")
        raise typer.Exit(1)

    prov = ctx.providers[provider]
    secrets: list[str] = []

    if file:
        try:
            lines = Path(file).read_text().splitlines()
        except OSError as exc:
            err_con.print(f"[red]Cannot read file:[/] {exc}")
            raise typer.Exit(1)
        secrets = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    elif keys:
        secrets = [k.strip() for k in keys.split(",") if k.strip()]
    elif from_env:
        for env in prov.env:
            val = os.environ.get(env, "").strip()
            if val:
                secrets.append(val)
                console.print(f"[dim]Read key from ${env}[/]")
    else:
        err_con.print("[red]Provide --file, --keys, or --from-env.[/]")
        raise typer.Exit(1)

    if not secrets:
        err_con.print("[yellow]No keys found.[/]")
        raise typer.Exit(1)

    for s in secrets:
        _add_one_key(ctx, provider, s, None, weight, rpm, tpm)
    console.print(f"[green]Imported {len(secrets)} key(s) for {prov.name}.[/]")
    ctx.close()


@key_app.command("list")
def key_list(provider: str = typer.Argument(None)):
    """Key health matrix: live/cooling/dead, burn rate, request counts."""
    ctx = load_ctx()
    keys = ctx.db.list_keys(provider=provider.lower() if provider else None)
    if not keys:
        console.print("[yellow]No keys stored.[/] Add one: `synckey key add <provider>`")
        ctx.close()
        return
    states = ctx.states.all()
    stats = ctx.db.key_stats()

    table = Table(title="Key health matrix")
    for col in ("id", "provider", "label", "weight", "health", "rpm cap", "rpm used", "requests", "errors", "cost"):
        table.add_column(col, no_wrap=True)

    for k in keys:
        st = states.get(k.id)
        from .state import Health as H, KeyState
        ks = st if st else KeyState()
        bucket = ctx.pool._buckets.get(k.id) if k.id in ctx.pool._buckets._buckets else None

        remaining = ks.cooldown_remaining()
        ht = health_text(ks.health, remaining)

        rpm_cap = k.rpm_limit or (ks.rpm_observed if ks.rpm_observed else None)
        rpm_cap_str = f"{rpm_cap:.0f}" if rpm_cap else "unknown"
        rpm_used = f"{bucket.emission_rpm():.1f}" if bucket else "0.0"

        s = stats.get(k.id, {})
        table.add_row(
            str(k.id),
            k.provider,
            k.label,
            str(k.weight),
            ht,
            rpm_cap_str,
            rpm_used,
            str(s.get("requests", 0)),
            str(s.get("errors", 0)),
            fmt_cost(s.get("cost")),
        )
    console.print(table)
    if any(states.get(k.id, None) and states[k.id].dead_reason for k in keys):
        console.print("[dim]Dead key reasons:[/]")
        for k in keys:
            if k.id in states and states[k.id].health == Health.DEAD:
                console.print(f"  [red]#{k.id}[/] {states[k.id].dead_reason}")
    ctx.close()


@key_app.command("rm")
def key_remove(key_id: int = typer.Argument(...)):
    """Remove a stored credential."""
    ctx = load_ctx()
    if ctx.db.remove_key(key_id):
        console.print(f"[green]Removed[/] key #{key_id}.")
    else:
        err_con.print(f"[red]No key #{key_id}.[/]")
    ctx.close()


@key_app.command("enable")
def key_enable(key_id: int = typer.Argument(...)):
    """Re-enable a dead or disabled key."""
    ctx = load_ctx()
    ctx.db.set_key_enabled(key_id, True)
    ctx.states.set_live(key_id)
    console.print(f"[green]Enabled[/] key #{key_id}.")
    ctx.close()


@key_app.command("disable")
def key_disable(key_id: int = typer.Argument(...)):
    """Disable a key without deleting it."""
    ctx = load_ctx()
    ctx.db.set_key_enabled(key_id, False)
    console.print(f"[yellow]Disabled[/] key #{key_id}.")
    ctx.close()


@key_app.command("limits")
def key_limits(
    key_id: int = typer.Argument(...),
    rpm: float = typer.Option(None, "--rpm"),
    tpm: float = typer.Option(None, "--tpm"),
):
    """Set or update RPM/TPM limits for a key (drives the proactive bucket)."""
    ctx = load_ctx()
    ctx.db.set_key_limits(key_id, rpm, tpm)
    console.print(f"[green]Updated[/] key #{key_id}: RPM={rpm or '(unchanged)'} TPM={tpm or '(unchanged)'}")
    ctx.close()


@app.command()
def models(
    provider: str = typer.Option(None, "--provider", "-p"),
    refresh: bool = typer.Option(False, "--refresh", "-r"),
    tier: str = typer.Option(None, "--tier", "-t", help="Filter by tier: frontier|high|mid|low"),
):
    """List the models your configured keys can actually call."""
    ctx = load_ctx()
    if refresh or not ctx.router.index:
        console.print("[dim]Querying providers...[/]")
        discovered = asyncio.run(ctx.router.refresh_index(ctx.first_secret))
        for pid, ms in discovered.items():
            color = "green" if ms else "red"
            console.print(f"  [{color}]{pid}[/]: {len(ms)} models")

    index = ctx.router.index
    if not index:
        console.print("[yellow]No models.[/] Add keys then run `synckey models -r`.")
        ctx.close()
        return

    filter_tier = TIER_NAMES.get({"frontier":4,"high":3,"mid":2,"low":1}.get(tier or "", 0))

    rows = []
    for model, provs in sorted(index.items()):
        t = model_tier(model)
        tier_name = TIER_NAMES.get(t or 0, "unknown")
        if tier and tier_name != tier:
            continue
        for pid in sorted(provs):
            if provider and pid != provider.lower():
                continue
            rows.append((model, pid, tier_name))

    table = Table(title=f"Usable models ({len(rows)})")
    table.add_column("model", style="cyan")
    table.add_column("provider")
    table.add_column("tier")
    for model, pid, tier_name in rows:
        color = {"frontier": "red", "high": "magenta", "mid": "yellow", "low": "dim"}.get(tier_name, "")
        table.add_row(model, pid, f"[{color}]{tier_name}[/]" if color else tier_name)
    console.print(table)
    console.print(f"[dim]Index age: {int(ctx.router.index_age())}s. Use --refresh to update.[/]")
    ctx.close()


@app.command()
def detect(model: str = typer.Argument(...)):
    """Show routing, tier, and floor for a model name."""
    ctx = load_ctx()
    res = ctx.router.resolve(model)
    tier = model_tier(model)
    price = model_price(model)
    if not res.providers:
        console.print(
            f"[yellow]'{model}' is unroutable.[/] No prefix, index entry, or pattern matched."
        )
    else:
        price_str = f"${price[0]:.2f}/${price[1]:.2f} per M tokens" if price else "unknown"
        console.print(
            Panel.fit(
                f"model:     [cyan]{model}[/]\n"
                f"upstream:  [cyan]{res.bare_model}[/]\n"
                f"routed by: [bold]{res.how}[/]\n"
                f"tier:      [bold]{TIER_NAMES.get(tier or 0, 'unknown')}[/] ({tier})\n"
                f"floor:     {TIER_NAMES.get(res.floor or 0, 'none')} (fallback stays >= this)\n"
                f"providers: {' > '.join(res.providers)}\n"
                f"price:     {price_str}",
                title="Routing",
                border_style="cyan",
            )
        )
    ctx.close()


@app.command()
def serve(
    host: str = typer.Option(None, "--host", "-h"),
    port: int = typer.Option(None, "--port", "-p"),
    workers: int = typer.Option(1, "--workers", help="Uvicorn worker count (1 for single-process async)."),
    reload: bool = typer.Option(False, "--reload"),
):
    """Run the unified OpenAI-compatible gateway."""
    import uvicorn
    from .server import create_app

    ctx = load_ctx()
    if not ctx.unified_key_hash():
        err_con.print("[red]Not initialized.[/] Run `synckey init` first.")
        raise typer.Exit(1)
    if not ctx.db.providers_with_keys():
        err_con.print("[yellow]Warning:[/] no provider keys stored yet. Add one: `synckey key add`.")

    bind_host = host or ctx.settings.host
    bind_port = port or ctx.settings.port

    states = ctx.states.all()
    from .state import Health as H
    live = sum(1 for s in states.values() if s.health == H.LIVE)
    dead = sum(1 for s in states.values() if s.health == H.DEAD)

    console.print(
        Panel.fit(
            f"[bold green]synckey gateway[/]\n\n"
            f"OpenAI base_url:  [cyan]http://{bind_host}:{bind_port}/v1[/]\n"
            f"providers:        {', '.join(ctx.db.providers_with_keys()) or '(none)'}\n"
            f"models indexed:   {len(ctx.router.index)}\n"
            f"keys live/dead:   {live}/{dead}\n"
            f"max_retries:      {ctx.settings.max_retries}\n"
            f"tier fallback:    {'on' if ctx.settings.tier_fallback_enabled else 'off'}\n"
            f"quality floor:    per-request via X-Quality-Floor header",
            border_style="green",
        )
    )
    uvicorn.run(
        create_app(ctx),
        host=bind_host,
        port=bind_port,
        workers=workers,
        reload=reload,
        log_level="warning",
        access_log=False,
    )


@app.command()
def usage(
    hours: float = typer.Option(None, "--hours"),
    recent: bool = typer.Option(False, "--recent"),
):
    """Token usage and request monitoring."""
    ctx = load_ctx()
    since = time.time() - hours * 3600 if hours else None

    if recent:
        table = Table(title="Recent requests")
        for col in ("when", "provider", "model", "tier", "tokens", "cost", "status", "ms"):
            table.add_column(col)
        for r in ctx.db.recent_usage(25):
            ago = int(time.time() - r["ts"])
            status = r["status_code"] or 0
            color = "green" if status == 200 else "red"
            tier_name = TIER_NAMES.get(r["tier"] or 0, "")
            table.add_row(
                f"{ago}s ago", r["provider"], r["model"], tier_name,
                fmt_num(r["total_tokens"]), fmt_cost(r["cost_usd"]),
                f"[{color}]{status}[/]", str(r["latency_ms"]),
            )
        console.print(table)
        ctx.close()
        return

    totals = ctx.db.usage_totals(since)
    console.print(
        Panel.fit(
            f"requests:  [bold]{totals['requests'] or 0}[/]  ([red]{totals['errors'] or 0} errors[/])\n"
            f"tokens:    [bold cyan]{fmt_num(totals['total_tokens'])}[/] total "
            f"({fmt_num(totals['prompt_tokens'])} prompt + {fmt_num(totals['completion_tokens'])} completion)\n"
            f"cost:      [bold green]{fmt_cost(totals['cost_usd'])}[/]",
            title=f"Usage{f' (last {hours}h)' if hours else ' (all time)'}",
            border_style="cyan",
        )
    )
    summary = ctx.db.usage_summary(since)
    if summary:
        table = Table(title="By model")
        for col in ("provider", "model", "tier", "reqs", "tokens", "cost", "errors", "avg ms"):
            table.add_column(col)
        for r in summary:
            tier_name = TIER_NAMES.get(r["tier"] or 0, "")
            table.add_row(
                r["provider"], r["model"], tier_name,
                str(r["requests"]), fmt_num(r["total_tokens"]),
                fmt_cost(r["cost_usd"]),
                str(r["errors"] or 0),
                f"{r['avg_latency']:.0f}" if r["avg_latency"] else "",
            )
        console.print(table)
    ctx.close()


@app.command()
def spend(hours: float = typer.Option(None, "--hours")):
    """Cost breakdown by provider and model."""
    ctx = load_ctx()
    since = time.time() - hours * 3600 if hours else None
    totals = ctx.db.usage_totals(since)
    summary = ctx.db.usage_summary(since)

    total_cost = totals["cost_usd"] or 0.0
    console.print(f"[bold]Total spend:[/] [green]{fmt_cost(total_cost)}[/]"
                  + (f" (last {hours}h)" if hours else " (all time)"))

    if not summary:
        console.print("[dim]No usage data.[/]")
        ctx.close()
        return

    table = Table(title="Spend breakdown")
    for col in ("provider", "model", "tier", "requests", "tokens", "cost", "% total"):
        table.add_column(col)
    for r in summary:
        cost = r["cost_usd"] or 0.0
        pct = (cost / total_cost * 100) if total_cost else 0
        tier_name = TIER_NAMES.get(r["tier"] or 0, "")
        table.add_row(
            r["provider"], r["model"], tier_name,
            str(r["requests"]), fmt_num(r["total_tokens"]),
            fmt_cost(cost), f"{pct:.1f}%",
        )
    console.print(table)
    ctx.close()


@app.command()
def events(
    type: str = typer.Option(None, "--type", "-t", help="Filter: rate_limited|key_dead|tier_fallback"),
    limit: int = typer.Option(50, "--limit", "-n"),
):
    """Routing events: rate limits, key deaths, tier fallbacks."""
    ctx = load_ctx()
    rows = ctx.db.recent_events(limit, event_type=type)
    if not rows:
        console.print("[dim]No events.[/]")
        ctx.close()
        return
    table = Table(title="Events")
    for col in ("when", "type", "provider", "model", "fallback_to", "message"):
        table.add_column(col)
    type_colors = {"rate_limited": "yellow", "key_dead": "red", "tier_fallback": "magenta"}
    for r in rows:
        ago = int(time.time() - r["ts"])
        etype = r["type"] or ""
        color = type_colors.get(etype, "")
        table.add_row(
            f"{ago}s ago",
            f"[{color}]{etype}[/]" if color else etype,
            r["provider"] or "",
            r["model"] or "",
            r["fallback_to"] or "",
            (r["message"] or "")[:80],
        )
    console.print(table)
    ctx.close()


@app.command()
def status():
    """At-a-glance overview."""
    ctx = load_ctx()
    initialized = bool(ctx.unified_key_hash())
    keys = ctx.db.list_keys()
    configured = ctx.db.providers_with_keys()
    totals = ctx.db.usage_totals()

    all_states = ctx.states.all()
    from .state import Health as H
    n_live = sum(1 for s in all_states.values() if s.health == H.LIVE)
    n_cool = sum(1 for s in all_states.values() if s.health == H.COOLING)
    n_dead = sum(1 for s in all_states.values() if s.health == H.DEAD)

    console.print(
        Panel.fit(
            f"home:         [cyan]{home()}[/]\n"
            f"initialized:  {'[green]yes[/]' if initialized else '[red]no, run synckey init[/]'}\n"
            f"providers:    {len(configured)} ({', '.join(configured) or 'none'})\n"
            f"keys:         {len(keys)} stored  [green]{n_live} live[/]  [yellow]{n_cool} cooling[/]  [red]{n_dead} dead[/]\n"
            f"models known: {len(ctx.router.index)}\n"
            f"lifetime:     {totals['requests'] or 0} requests, "
            f"{fmt_num(totals['total_tokens'])} tokens, {fmt_cost(totals['cost_usd'])}\n"
            f"gateway:      http://{ctx.settings.host}:{ctx.settings.port}/v1\n"
            f"quality floor: auto (inferred from model tier) + X-Quality-Floor header",
            title=f"synckey {__version__}",
            border_style="magenta",
        )
    )
    ctx.close()


@app.command()
def test(provider: str = typer.Argument(None)):
    """Health-check stored keys by hitting each provider's models endpoint."""
    import httpx

    ctx = load_ctx()
    targets = [provider.lower()] if provider else ctx.db.providers_with_keys()
    if not targets:
        console.print("[yellow]No providers to test.[/]")
        ctx.close()
        return

    table = Table(title="Key health check")
    for col in ("provider", "key", "current health", "api result"):
        table.add_column(col)

    all_states = ctx.states.all()
    for pid in targets:
        prov = ctx.providers.get(pid)
        if not prov:
            table.add_row(pid, "", "", "[red]unknown provider[/]")
            continue
        url = prov.base_url.rstrip("/") + prov.models_path
        for k in ctx.db.list_keys(provider=pid, enabled_only=True):
            st = all_states.get(k.id)
            from .state import KeyState
            ks = st if st else KeyState()
            cur = health_text(ks.health, ks.cooldown_remaining())
            secret = ctx.box.open(k.secret)
            try:
                resp = httpx.get(
                    url,
                    headers=prov.auth_headers(secret),
                    params=prov.auth_params(secret),
                    timeout=20.0,
                )
                if resp.status_code == 200:
                    result = "[green]ok[/]"
                elif resp.status_code in (401, 403):
                    result = "[red]auth failed (key dead/expired)[/]"
                    ctx.pool.on_dead(k.id, f"HTTP {resp.status_code} during health check")
                elif resp.status_code == 429:
                    result = "[yellow]rate limited[/]"
                else:
                    result = f"[red]HTTP {resp.status_code}[/]"
            except httpx.HTTPError as exc:
                result = f"[red]{type(exc).__name__}[/]"
            table.add_row(pid, f"#{k.id} {k.label}", cur, result)
    console.print(table)
    ctx.close()


@app.command()
def dash(interval: float = typer.Option(2.0, "--interval", "-i", help="Refresh interval (seconds).")):
    """Live dashboard: key health, burn rates, and recent events. Press Ctrl+C to exit."""
    from .state import Health as H, KeyState

    ctx = load_ctx()

    def _header() -> Panel:
        totals = ctx.db.usage_totals()
        all_st = ctx.states.all()
        n_live = sum(1 for s in all_st.values() if s.health == H.LIVE)
        n_cool = sum(1 for s in all_st.values() if s.health == H.COOLING)
        n_dead = sum(1 for s in all_st.values() if s.health == H.DEAD)
        return Panel(
            f"[bold]{totals['requests'] or 0}[/] requests  "
            f"[cyan]{fmt_num(totals['total_tokens'])}[/] tokens  "
            f"[green]{fmt_cost(totals['cost_usd'])}[/] spent    "
            f"keys: [green]{n_live} live[/]  [yellow]{n_cool} cooling[/]  [red]{n_dead} dead[/]",
            title=f"[bold]synckey {__version__}[/]  (Ctrl+C to exit)",
        )

    def _keys_panel() -> Panel:
        keys = ctx.db.list_keys()
        stats = ctx.db.key_stats()
        all_st = ctx.states.all()
        t = Table(show_header=True, header_style="bold", expand=True, box=None)
        for col in ("#", "provider", "health", "rpm", "reqs", "cost"):
            t.add_column(col)
        for k in keys:
            st = all_st.get(k.id)
            ks = st if st else KeyState()
            remaining = ks.cooldown_remaining()
            ht = health_text(ks.health, remaining)
            bucket = ctx.pool._buckets._buckets.get(k.id)
            rpm_used = f"{bucket.emission_rpm():.1f}" if bucket else "0.0"
            s = stats.get(k.id, {})
            t.add_row(str(k.id), k.provider, ht, rpm_used, str(s.get("requests", 0)), fmt_cost(s.get("cost")))
        return Panel(t, title="Keys")

    def _events_panel() -> Panel:
        rows = ctx.db.recent_events(10)
        t = Table(show_header=True, header_style="bold", expand=True, box=None)
        for col in ("when", "type", "model", "note"):
            t.add_column(col)
        colors = {"rate_limited": "yellow", "key_dead": "red", "tier_fallback": "magenta"}
        for r in rows:
            ago = int(time.time() - r["ts"])
            etype = r["type"] or ""
            color = colors.get(etype, "")
            t.add_row(
                f"{ago}s",
                f"[{color}]{etype}[/]" if color else etype,
                r["model"] or "",
                (r["message"] or "")[:40],
            )
        return Panel(t, title="Events")

    def _requests_panel() -> Panel:
        t = Table(show_header=True, header_style="bold", expand=True, box=None)
        for col in ("when", "provider", "model", "status", "tokens", "ms"):
            t.add_column(col)
        for r in ctx.db.recent_usage(6):
            ago = int(time.time() - r["ts"])
            status = r["status_code"] or 0
            color = "green" if status == 200 else "red"
            t.add_row(
                f"{ago}s", r["provider"], r["model"],
                f"[{color}]{status}[/]", fmt_num(r["total_tokens"]), str(r["latency_ms"] or 0),
            )
        return Panel(t, title="Recent requests")

    def _build() -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=9),
        )
        layout["body"].split_row(Layout(name="keys", ratio=3), Layout(name="events", ratio=2))
        layout["header"].update(_header())
        layout["body"]["keys"].update(_keys_panel())
        layout["body"]["events"].update(_events_panel())
        layout["footer"].update(_requests_panel())
        return layout

    try:
        with Live(_build(), refresh_per_second=1, screen=True) as live:
            while True:
                time.sleep(interval)
                live.update(_build())
    except KeyboardInterrupt:
        pass
    finally:
        ctx.close()


@app.command()
def version():
    """Print the synckey version."""
    console.print(f"synckey {__version__}")


if __name__ == "__main__":
    app()
