"""synckey command-line interface.

  Getting started
    synckey setup                 guided first-run: key, providers, models, defaults
    synckey init                  one-time setup; prints your unified key
    synckey key add <provider>    store credential(s): single, comma-bulk, or --file
    synckey serve                 run the unified gateway

  Configuration
    synckey config                show defaults, aliases, settings
    synckey config set-default <model>            global default model
    synckey config provider-default <prov> <model>  per-provider default
    synckey config alias <name> <target>          define a client-facing alias
    synckey tier set <model> <tier>               override a model's quality tier

  Keys & models
    synckey providers             list every supported provider
    synckey key list|rm|enable|disable|limits
    synckey models [--refresh]    discover models your keys can call
    synckey detect <model>        show routing and tier

  Monitoring
    synckey dash                  live dashboard (1/2/3 views, p/r/q keys)
    synckey usage [--recent]      token and cost monitoring
    synckey spend                 cost breakdown by model
    synckey events [--type]       routing events log
    synckey deferred list|get     inspect the deferred request queue
    synckey status                at-a-glance overview
    synckey test [provider]       health-check stored keys
"""

from __future__ import annotations

import asyncio
import os
import sys
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
from .tiers import TIER_BY_NAME, TIER_NAMES, model_tier, model_price

app = typer.Typer(
    name="synckey",
    help="Merge every AI provider key behind one unified, OpenAI-compatible API key.",
    no_args_is_help=True,
    add_completion=False,
)
key_app = typer.Typer(help="Manage stored provider credentials.", no_args_is_help=True)
app.add_typer(key_app, name="key")
config_app = typer.Typer(help="View and edit routing preferences (defaults, aliases).")
app.add_typer(config_app, name="config")
tier_app = typer.Typer(help="Inspect and override model quality tiers.", no_args_is_help=True)
app.add_typer(tier_app, name="tier")

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
            f"Next: [bold]synckey key add groq[/]  then  [bold]synckey serve[/]\n"
            f"Or run the guided flow: [bold]synckey setup[/]",
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


def _read_key_file(path: Path) -> list[str]:
    """Read keys from a file: one per line and/or comma-separated, '#' comments ignored."""
    try:
        text = Path(path).read_text()
    except OSError as exc:
        err_con.print(f"[red]Cannot read file:[/] {exc}")
        raise typer.Exit(1)
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.extend(s.strip() for s in line.split(",") if s.strip())
    return out


def _collect_secrets(ctx, provider: str, key, file, from_env) -> list[str]:
    """Gather one or many secrets from --key (comma ok), --file, --from-env, or a prompt."""
    prov = ctx.providers[provider]
    secrets: list[str] = []
    if key:
        secrets = [s.strip() for s in key.split(",") if s.strip()]
    elif file:
        secrets = _read_key_file(file)
    elif from_env:
        for env in prov.env:
            val = os.environ.get(env, "").strip()
            if val:
                secrets.append(val)
                console.print(f"[dim]Read key from ${env}[/]")
        if not secrets:
            err_con.print(f"[red]No key in env vars:[/] {', '.join(prov.env) or '(none)'}")
            raise typer.Exit(1)
    else:
        entered = typer.prompt(
            f"{prov.name} API key (one key, comma-separated keys, or a file path)",
            hide_input=True,
        ).strip()
        candidate = Path(entered)
        if entered and candidate.exists() and candidate.is_file():
            secrets = _read_key_file(candidate)
        else:
            secrets = [s.strip() for s in entered.split(",") if s.strip()]
    if not secrets:
        err_con.print("[red]No keys provided.[/]")
        raise typer.Exit(1)
    return secrets


async def _models_per_key(ctx, provider: str) -> dict[int, list[str]]:
    """Query each enabled key's /models endpoint; returns {key_id: [models]}."""
    import httpx

    keys = ctx.db.list_keys(provider=provider, enabled_only=True)
    out: dict[int, list[str]] = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for k in keys:
            try:
                secret = ctx.box.open(k.secret)
                out[k.id] = await ctx.router._fetch_models(client, provider, secret)
            except Exception:
                out[k.id] = []
    return out


def _show_models_per_key(ctx, provider: str, per_key: dict[int, list[str]]) -> list[str]:
    """Print fetched models (per-key when keys differ) and return the union."""
    keys = {k.id: k for k in ctx.db.list_keys(provider=provider)}
    sets = {kid: set(ms) for kid, ms in per_key.items()}
    union = sorted(set().union(*sets.values())) if sets else []
    differ = len({frozenset(s) for s in sets.values()}) > 1

    if not union:
        console.print(f"[yellow]No models returned for {provider}.[/] Check the key(s).")
        return []

    if differ:
        console.print(f"[yellow]Keys for {provider} expose different model sets:[/]")
        common = set.intersection(*sets.values()) if sets else set()
        for kid, ms in sets.items():
            lbl = keys[kid].label if kid in keys else f"#{kid}"
            extra = sorted(ms - common)
            console.print(
                f"  [cyan]#{kid}[/] {lbl}: {len(ms)} models"
                + (f"  [dim](unique: {', '.join(extra[:6])}{'...' if len(extra) > 6 else ''})[/]" if extra else "")
            )
    table = Table(title=f"{provider}: {len(union)} models")
    table.add_column("model", style="cyan")
    table.add_column("tier")
    for m in union:
        tn = TIER_NAMES.get(model_tier(m) or 0, "")
        table.add_row(m, tn)
    console.print(table)
    return union


def _post_add_flow(ctx, provider: str, fetch_opt: bool | None, default_opt: str | None) -> None:
    """After keys are added: optionally fetch models, then set this provider's default."""
    interactive = sys.stdin.isatty()
    union: list[str] = []

    do_fetch = fetch_opt
    if do_fetch is None:
        do_fetch = (
            typer.confirm(f"Fetch available models for {provider} now?", default=True)
            if interactive
            else False
        )

    if do_fetch:
        console.print(f"[dim]Querying {provider}...[/]")
        per_key = asyncio.run(_models_per_key(ctx, provider))
        union = _show_models_per_key(ctx, provider, per_key)
        if union:
            ctx.router.merge_provider_models(provider, union)

    default = default_opt
    if default is None and interactive:
        prompt_txt = f"Default model for {provider} (model id / alias / tier, blank to skip)"
        default = typer.prompt(prompt_txt, default="", show_default=False).strip() or None

    if default:
        if union and default not in union and default.lower() not in ("frontier", "high", "mid", "low"):
            console.print(f"[yellow]Note:[/] '{default}' wasn't in the fetched list; saving anyway.")
        ctx.prefs.set_provider_default(provider, default)
        console.print(f"[green]Set {provider} default ->[/] [cyan]{default}[/]")
        if not ctx.prefs.global_default():
            if not interactive or typer.confirm(
                f"No global default set. Use '{default}' as the global default too?", default=True
            ):
                ctx.prefs.set_global_default(default)
                console.print(f"[green]Set global default ->[/] [cyan]{default}[/]")


@key_app.command("add")
def key_add(
    provider: str = typer.Argument(..., help="Provider id, e.g. groq, gemini, cohere."),
    key: str = typer.Option(None, "--key", "-k", help="Secret key. Comma-separate for bulk."),
    file: Path = typer.Option(None, "--file", "-f", help="File of keys (per line or comma-separated)."),
    label: str = typer.Option(None, "--label", "-l"),
    weight: int = typer.Option(1, "--weight", "-w"),
    from_env: bool = typer.Option(False, "--from-env", help="Read the key from this provider's env vars."),
    rpm: float = typer.Option(None, "--rpm", help="Known RPM limit for this key."),
    tpm: float = typer.Option(None, "--tpm", help="Known TPM limit for this key."),
    fetch: bool = typer.Option(None, "--fetch/--no-fetch", help="Fetch models after adding (default: ask)."),
    default: str = typer.Option(None, "--default", help="Set this provider's default model."),
):
    """Store credential(s) for a provider.

    Accepts a single key, comma-separated keys (--key "a,b,c"), or a file
    (--file keys.txt). After adding, offers to fetch the models those keys can
    call and to set this provider's default model.
    """
    ctx = load_ctx()
    provider = provider.lower()
    if provider not in ctx.providers:
        err_con.print(f"[red]Unknown provider '{provider}'.[/] Run `synckey providers`.")
        raise typer.Exit(1)

    secrets = _collect_secrets(ctx, provider, key, file, from_env)
    for i, s in enumerate(secrets):
        lbl = label if len(secrets) == 1 else (f"{label}-{i+1}" if label else None)
        _add_one_key(ctx, provider, s, lbl, weight, rpm, tpm)
    if len(secrets) > 1:
        console.print(f"[green]Added {len(secrets)} key(s) for {ctx.providers[provider].name}.[/]")

    _post_add_flow(ctx, provider, fetch, default)
    ctx.close()


@key_app.command("import", hidden=True)
def key_import(
    provider: str = typer.Argument(..., help="Provider id, e.g. groq, gemini."),
    file: Path = typer.Option(None, "--file", "-f", help="File with keys (per line or comma-separated)."),
    keys: str = typer.Option(None, "--keys", "-k", help="Comma-separated keys."),
    from_env: bool = typer.Option(False, "--from-env", help="Import all matching env vars."),
    weight: int = typer.Option(1, "--weight", "-w"),
    rpm: float = typer.Option(None, "--rpm"),
    tpm: float = typer.Option(None, "--tpm"),
):
    """Deprecated alias for `synckey key add` (which now handles --file and bulk)."""
    ctx = load_ctx()
    provider = provider.lower()
    if provider not in ctx.providers:
        err_con.print(f"[red]Unknown provider '{provider}'.[/] Run `synckey providers`.")
        raise typer.Exit(1)
    secrets = _collect_secrets(ctx, provider, keys, file, from_env)
    for s in secrets:
        _add_one_key(ctx, provider, s, None, weight, rpm, tpm)
    console.print(
        f"[green]Imported {len(secrets)} key(s) for {ctx.providers[provider].name}.[/] "
        "[dim](tip: `synckey key add` now does this directly)[/]"
    )
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
        from .state import KeyState
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


@config_app.callback(invoke_without_command=True)
def config_main(ctx_: typer.Context):
    """Show routing preferences when called with no subcommand."""
    if ctx_.invoked_subcommand is not None:
        return
    ctx = load_ctx()
    s = ctx.settings
    p = ctx.prefs
    lines = [
        f"global default:   [cyan]{p.global_default() or '(none)'}[/]",
        f"gateway:          http://{s.host}:{s.port}",
        f"provider priority:{(' ' + ' > '.join(s.provider_priority)) if s.provider_priority else ' (none)'}",
        f"tier fallback:    {'on' if s.tier_fallback_enabled else 'off'}",
        f"cross-provider:   {'on' if s.cross_provider_fallback else 'off'}",
        f"deferred queue:   {'on' if s.deferred_enabled else 'off'} (TTL {int(s.deferred_ttl)}s)",
    ]
    console.print(Panel.fit("\n".join(lines), title="Settings", border_style="cyan"))

    pdefs = p.provider_defaults()
    if pdefs:
        t = Table(title="Per-provider defaults")
        t.add_column("provider", style="cyan")
        t.add_column("default model")
        for prov, model in sorted(pdefs.items()):
            t.add_row(prov, model)
        console.print(t)

    aliases = p.aliases()
    if aliases:
        t = Table(title="Aliases")
        t.add_column("alias", style="cyan")
        t.add_column("-> target")
        for name, target in sorted(aliases.items()):
            t.add_row(name, target)
        console.print(t)

    tov = {**s.tier_overrides, **p.tier_overrides()}
    if tov:
        t = Table(title="Tier overrides")
        t.add_column("model", style="cyan")
        t.add_column("tier")
        for model, tier in sorted(tov.items()):
            t.add_row(model, str(tier))
        console.print(t)

    if not pdefs and not aliases:
        console.print(
            "[dim]No defaults or aliases yet. Try `synckey config set-default <model>` "
            "or `synckey config alias fast mid`.[/]"
        )
    ctx.close()


@config_app.command("set-default")
def config_set_default(model: str = typer.Argument(..., help="Model id, alias, or tier.")):
    """Set the global default model (used when a request names no model)."""
    ctx = load_ctx()
    ctx.prefs.set_global_default(model)
    console.print(f"[green]Global default ->[/] [cyan]{model}[/]")
    ctx.close()


@config_app.command("provider-default")
def config_provider_default(
    provider: str = typer.Argument(..., help="Provider id, e.g. groq."),
    model: str = typer.Argument(..., help="Model id, alias, or tier."),
):
    """Set a provider's default model (used when a request names only that provider)."""
    ctx = load_ctx()
    if provider.lower() not in ctx.providers:
        err_con.print(f"[red]Unknown provider '{provider}'.[/]")
        raise typer.Exit(1)
    ctx.prefs.set_provider_default(provider, model)
    console.print(f"[green]{provider} default ->[/] [cyan]{model}[/]")
    ctx.close()


@config_app.command("alias")
def config_alias(
    name: str = typer.Argument(..., help="Alias name, e.g. fast."),
    target: str = typer.Argument(..., help="A model id or tier (frontier|high|mid|low)."),
):
    """Define an alias that clients can send as the model (e.g. fast -> mid)."""
    ctx = load_ctx()
    ctx.prefs.set_alias(name, target)
    console.print(f"[green]Alias[/] [cyan]{name}[/] -> [cyan]{target}[/]")
    ctx.close()


@config_app.command("unalias")
def config_unalias(name: str = typer.Argument(...)):
    """Remove an alias."""
    ctx = load_ctx()
    if ctx.prefs.remove_alias(name):
        console.print(f"[green]Removed alias[/] {name}.")
    else:
        err_con.print(f"[yellow]No alias '{name}'.[/]")
    ctx.close()


@tier_app.command("set")
def tier_set(
    model: str = typer.Argument(..., help="Model id (or substring you call)."),
    tier: str = typer.Argument(..., help="frontier | high | mid | low"),
):
    """Override a model's quality tier (persists; overrides auto-detection)."""
    ctx = load_ctx()
    if tier.lower() not in TIER_BY_NAME:
        err_con.print(f"[red]Tier must be one of:[/] {', '.join(TIER_BY_NAME)}")
        raise typer.Exit(1)
    ctx.prefs.set_tier_override(model, tier.lower())
    console.print(f"[green]Tier[/] [cyan]{model}[/] -> [bold]{tier.lower()}[/]")
    ctx.close()


@tier_app.command("unset")
def tier_unset(model: str = typer.Argument(...)):
    """Remove a manual tier override (revert to auto-detection)."""
    ctx = load_ctx()
    if ctx.prefs.remove_tier_override(model):
        console.print(f"[green]Reverted[/] {model} to auto-detected tier.")
    else:
        err_con.print(f"[yellow]No override for '{model}'.[/]")
    ctx.close()


@tier_app.command("list")
def tier_list():
    """Show manual tier overrides."""
    ctx = load_ctx()
    overrides = {**ctx.settings.tier_overrides, **ctx.prefs.tier_overrides()}
    if not overrides:
        console.print("[dim]No tier overrides. All tiers are auto-detected.[/]")
        console.print("[dim]Set one: `synckey tier set my-model frontier`[/]")
        ctx.close()
        return
    t = Table(title="Tier overrides")
    t.add_column("model", style="cyan")
    t.add_column("tier")
    for model, tier in sorted(overrides.items()):
        t.add_row(model, str(tier))
    console.print(t)
    ctx.close()


@app.command()
def setup():
    """Guided first-run: mint your key, add providers, fetch models, set defaults."""
    ensure_home()
    ctx = Context()

    # 1. Unified key
    if not ctx.unified_key_hash():
        unified = generate_unified_key()
        ctx.db.set_meta("unified_key_hash", sha256(unified))
        ctx.db.set_meta("created_at", str(time.time()))
        _ = ctx.box
        console.print(
            Panel.fit(
                f"Your unified API key (shown once — store it now):\n\n  [bold cyan]{unified}[/]",
                title="Unified key minted",
                border_style="green",
            )
        )
    else:
        console.print("[dim]Already initialized — keeping your existing unified key.[/]")

    # 2. Add providers
    console.print("\n[bold]Add provider keys.[/] Enter a provider id (e.g. groq, gemini) or blank to finish.")
    while True:
        provider = typer.prompt("Provider", default="", show_default=False).strip().lower()
        if not provider:
            break
        if provider not in ctx.providers:
            err_con.print(f"[red]Unknown provider '{provider}'.[/] Run `synckey providers` for the list.")
            continue
        secrets = _collect_secrets(ctx, provider, None, None, False)
        for s in secrets:
            _add_one_key(ctx, provider, s, None, 1, None, None)
        _post_add_flow(ctx, provider, None, None)
        console.print()

    # 3. Global default
    if not ctx.prefs.global_default():
        pick = typer.prompt(
            "Global default model (model id / alias / tier, blank to skip)",
            default="", show_default=False,
        ).strip()
        if pick:
            ctx.prefs.set_global_default(pick)
            console.print(f"[green]Global default ->[/] [cyan]{pick}[/]")

    # 4. Summary
    configured = ctx.db.providers_with_keys()
    console.print(
        Panel.fit(
            f"providers:      {', '.join(configured) or '(none)'}\n"
            f"models indexed: {len(ctx.router.index)}\n"
            f"global default: {ctx.prefs.global_default() or '(none)'}\n\n"
            f"Start the gateway:  [bold]synckey serve[/]\n"
            f"Health-check keys:  [bold]synckey test[/]\n"
            f"Watch it live:      [bold]synckey dash[/]",
            title="Setup complete",
            border_style="green",
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
            f"default model:    {ctx.prefs.global_default() or '(none — clients must send model)'}\n"
            f"max_retries:      {ctx.settings.max_retries}\n"
            f"tier fallback:    {'on' if ctx.settings.tier_fallback_enabled else 'off'}\n"
            f"deferred queue:   {'on' if ctx.settings.deferred_enabled else 'off'}"
            f" (202 + poll when all keys cooling; TTL {int(ctx.settings.deferred_ttl)}s)\n"
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


deferred_app = typer.Typer(help="Inspect the deferred request queue.", no_args_is_help=True)
app.add_typer(deferred_app, name="deferred")


@deferred_app.command("list")
def deferred_list(limit: int = typer.Option(25, "--limit", "-n")):
    """Show queued/running/done deferred requests."""
    ctx = load_ctx()
    rows = ctx.db.recent_deferred(limit)
    if not rows:
        console.print("[dim]No deferred requests.[/]")
        ctx.close()
        return
    colors = {"queued": "yellow", "running": "cyan", "done": "green", "error": "red"}
    t = Table(title="Deferred queue")
    for col in ("id", "status", "model", "age", "eta", "code"):
        t.add_column(col)
    now = time.time()
    for r in rows:
        status_ = r["status"]
        eta = r["eta"] or 0
        eta_str = f"{int(eta - now)}s" if status_ in ("queued", "running") and eta > now else ""
        t.add_row(
            r["id"][:18],
            f"[{colors.get(status_, '')}]{status_}[/]" if colors.get(status_) else status_,
            r["model"] or "",
            f"{int(now - r['created_at'])}s",
            eta_str,
            str(r["status_code"] or ""),
        )
    console.print(t)
    console.print(f"[dim]TTL after completion: {int(ctx.settings.deferred_ttl)}s. Poll: GET /v1/requests/<id>[/]")
    ctx.close()


@deferred_app.command("get")
def deferred_get(request_id: str = typer.Argument(...)):
    """Show a deferred request's status and stored response (if done)."""
    ctx = load_ctx()
    r = ctx.db.get_deferred(request_id)
    if r is None:
        err_con.print("[red]Unknown or expired request id.[/]")
        raise typer.Exit(1)
    console.print(
        Panel.fit(
            f"id:      [cyan]{r['id']}[/]\n"
            f"status:  [bold]{r['status']}[/]\n"
            f"model:   {r['model'] or ''}\n"
            f"code:    {r['status_code'] or ''}\n"
            f"attempts:{r['attempts']}",
            title="Deferred request",
            border_style="cyan",
        )
    )
    if r["status"] == "done" and r["response"]:
        body = r["response"].decode("utf-8", "replace")
        console.print(body[:2000] + ("..." if len(body) > 2000 else ""))
    elif r["status"] == "error":
        console.print(f"[red]{r['error'] or ''}[/]")
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
            f"default model:{(' ' + ctx.prefs.global_default()) if ctx.prefs.global_default() else ' (none)'}\n"
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


def _read_key(timeout: float) -> str | None:
    """Poll for a single keypress for up to `timeout` seconds. Windows: msvcrt.

    Returns the lowercased character, or None if nothing was pressed (or the
    platform has no non-blocking console read — then it just sleeps).
    """
    try:
        import msvcrt  # Windows only
    except ImportError:
        time.sleep(timeout)
        return None
    end = time.time() + timeout
    while time.time() < end:
        if msvcrt.kbhit():
            try:
                ch = msvcrt.getwch()
            except Exception:
                return None
            return ch.lower()
        time.sleep(0.05)
    return None


@app.command()
def dash(interval: float = typer.Option(2.0, "--interval", "-i", help="Refresh interval (seconds).")):
    """Live dashboard with keyboard navigation.

    Keys: [1] overview  [2] keys  [3] events  [p] pause  [r] refresh  [q] quit.
    """
    from .state import Health as H, KeyState

    ctx = load_ctx()
    interactive = sys.stdin.isatty()

    def _header(view: str, paused: bool) -> Panel:
        totals = ctx.db.usage_totals()
        all_st = ctx.states.all()
        n_live = sum(1 for s in all_st.values() if s.health == H.LIVE)
        n_cool = sum(1 for s in all_st.values() if s.health == H.COOLING)
        n_dead = sum(1 for s in all_st.values() if s.health == H.DEAD)
        default = ctx.prefs.global_default() or "(none)"
        state = "[yellow]PAUSED[/]" if paused else "[green]live[/]"
        return Panel(
            f"[bold]{totals['requests'] or 0}[/] requests  "
            f"[cyan]{fmt_num(totals['total_tokens'])}[/] tokens  "
            f"[green]{fmt_cost(totals['cost_usd'])}[/] spent    "
            f"keys: [green]{n_live} live[/]  [yellow]{n_cool} cooling[/]  [red]{n_dead} dead[/]    "
            f"default: [cyan]{default}[/]",
            title=f"[bold]synckey {__version__}[/]  ·  view: [bold]{view}[/]  ·  {state}",
        )

    def _legend() -> Panel:
        hint = (
            "[bold]1[/] overview   [bold]2[/] keys   [bold]3[/] events   "
            "[bold]p[/] pause   [bold]r[/] refresh   [bold]q[/] quit"
            if interactive
            else "Ctrl+C to exit  ·  (interactive keys unavailable in this terminal)"
        )
        return Panel(hint, style="dim")

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

    def _build(view: str, paused: bool) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="legend", size=3),
        )
        layout["header"].update(_header(view, paused))
        layout["legend"].update(_legend())
        if view == "keys":
            layout["body"].split_column(
                Layout(_keys_panel(), name="keys", ratio=2),
                Layout(_requests_panel(), name="requests", ratio=1),
            )
        elif view == "events":
            layout["body"].update(_events_panel())
        else:  # overview
            layout["body"].split_column(Layout(name="top"), Layout(name="bottom", size=9))
            layout["body"]["top"].split_row(
                Layout(_keys_panel(), name="keys", ratio=3),
                Layout(_events_panel(), name="events", ratio=2),
            )
            layout["body"]["bottom"].update(_requests_panel())
        return layout

    view = "overview"
    paused = False
    views = {"1": "overview", "2": "keys", "3": "events"}
    last = 0.0
    try:
        with Live(_build(view, paused), refresh_per_second=4, screen=True) as live:
            while True:
                now = time.time()
                if not paused and now - last >= interval:
                    live.update(_build(view, paused))
                    last = now
                ch = _read_key(0.2)
                if ch is None:
                    continue
                if ch in ("q", "\x1b"):  # q or ESC
                    break
                elif ch == "p":
                    paused = not paused
                    live.update(_build(view, paused))
                elif ch == "r":
                    live.update(_build(view, paused))
                    last = now
                elif ch in views:
                    view = views[ch]
                    live.update(_build(view, paused))
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
