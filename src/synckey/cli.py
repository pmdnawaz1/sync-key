"""synckey command-line interface."""

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
from .config import ensure_home, generate_unified_key, home, config_path
from .context import Context
from .db import sha256
from .state import Health
from .tiers import TIER_BY_NAME, TIER_NAMES, model_tier, model_price

app = typer.Typer(
    name="synckey",
    help="Merge every AI provider key behind one unified, OpenAI-compatible API key.",
    no_args_is_help=False,
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def synckey_main(ctx: typer.Context, guide: bool = typer.Option(False, "--guide", help="Interactive walkthrough of synckey concepts.")):
    """synckey merge every AI provider behind one key."""
    if guide:
        _run_guide()
        raise typer.Exit(0)
    if ctx.invoked_subcommand is not None:
        return
    ctx.command.get_help(ctx)


config_app = typer.Typer(help="View and edit routing preferences (defaults, aliases).")
app.add_typer(config_app, name="config")
tier_app = typer.Typer(help="Inspect and override model quality tiers.", no_args_is_help=True)
app.add_typer(tier_app, name="tier")
routing_app = typer.Typer(help="Routing preferences: provider priority and fallback presets.")
app.add_typer(routing_app, name="routing")

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
        example = union[0] if union else "llama-3.3-70b"
        prompt_txt = f"Default model for {provider} (model id like '{example}', or tier: frontier/high/mid/low)"
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
def config_set_default(model: str = typer.Argument(..., help="Model id (e.g. gpt-4o), a tier (frontier/high/mid/low), or an alias.")):
    """Set the global default model (used when a request names no model)."""
    ctx = load_ctx()
    ctx.prefs.set_global_default(model)
    console.print(f"[green]Global default ->[/] [cyan]{model}[/]")
    ctx.close()


@config_app.command("provider-default")
def config_provider_default(
    provider: str = typer.Argument(..., help="Provider id, e.g. groq."),
    model: str = typer.Argument(..., help="Model id (e.g. llama-3.3-70b), a tier (frontier/high/mid/low), or an alias."),
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


@app.command("keys")
def keys(
    provider: str = typer.Argument(None),
    rm: int = typer.Option(None, "--rm", help="Remove a key by its id."),
    enable: int = typer.Option(None, "--enable", help="Re-enable a dead or disabled key."),
    disable: int = typer.Option(None, "--disable", help="Disable a key without deleting it."),
):
    """List all stored keys, or manage them with flags.

    Examples:
      synckey keys              list all keys
      synckey keys --rm 3       remove key #3
      synckey keys --enable 3   re-enable key #3
      synckey keys --disable 3  disable key #3
    """
    ctx = load_ctx()

    if rm is not None:
        if ctx.db.remove_key(rm):
            console.print(f"[green]Removed[/] key #{rm}.")
        else:
            err_con.print(f"[red]No key #{rm}.[/]")
        ctx.close()
        return

    if enable is not None:
        ctx.db.set_key_enabled(enable, True)
        ctx.states.set_live(enable)
        console.print(f"[green]Enabled[/] key #{enable}.")
        ctx.close()
        return

    if disable is not None:
        ctx.db.set_key_enabled(disable, False)
        console.print(f"[yellow]Disabled[/] key #{disable}.")
        ctx.close()
        return

    keys = ctx.db.list_keys(provider=provider.lower() if provider else None)
    if not keys:
        console.print("[yellow]No keys stored.[/] Run `synckey setup` to add one.")
        ctx.close()
        return

    states = ctx.states.all()
    stats = ctx.db.key_stats()

    table = Table(title="Keys  (remove: synckey keys --rm <id>)")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("provider")
    table.add_column("health")
    table.add_column("requests", justify="right")
    table.add_column("errors", justify="right")
    table.add_column("cost", justify="right")

    for k in keys:
        st = states.get(k.id)
        from .state import KeyState
        ks = st if st else KeyState()
        ht = health_text(ks.health, ks.cooldown_remaining())
        s = stats.get(k.id, {})
        table.add_row(
            str(k.id),
            k.provider,
            ht,
            str(s.get("requests", 0)),
            str(s.get("errors", 0)),
            fmt_cost(s.get("cost")),
        )

    console.print(table)
    ctx.close()


@app.command()
def setup(force: bool = typer.Option(False, "--force", help="Regenerate the unified API key.")):
    """Add or update provider API keys (interactive guided setup)."""
    ensure_home()
    ctx = Context()

    # 1. Unified key
    if force:
        unified = generate_unified_key()
        ctx.db.set_meta("unified_key_hash", sha256(unified))
        ctx.db.set_meta("created_at", str(time.time()))
        _ = ctx.box
        console.print(
            Panel.fit(
                f"Your new unified API key (shown once — store it now):\n\n  [bold cyan]{unified}[/]",
                title="Unified key regenerated",
                border_style="green",
            )
        )
    elif not ctx.unified_key_hash():
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
    console.print("\n[bold]Add provider keys.[/]")
    added_any = False
    while True:
        if added_any:
            prompt = "Add another provider and keys?"
        else:
            prompt = "Add a provider and keys?"
        add_more = (
            typer.confirm(prompt, default=False)
            if sys.stdin.isatty()
            else False
        )
        if not add_more:
            break
        provider = typer.prompt(
            "Provider id (e.g. groq, gemini, openai)",
            default="", show_default=False,
        ).strip().lower()
        if not provider:
            break
        if provider not in ctx.providers:
            err_con.print(f"[red]Unknown provider '{provider}'.[/] Run `synckey providers` for the list.")
            continue
        secrets = _collect_secrets(ctx, provider, None, None, False)
        for s in secrets:
            _add_one_key(ctx, provider, s, None, 1, None, None)
        _post_add_flow(ctx, provider, None, None)
        added_any = True
        console.print()
    if not ctx.prefs.global_default():
        pick = typer.prompt(
            "Global default model (model id like 'gpt-4o', or a tier: frontier/high/mid/low, or an alias)",
            default="", show_default=False,
        ).strip()
        if pick:
            ctx.prefs.set_global_default(pick)
            console.print(f"[green]Global default ->[/] [cyan]{pick}[/]")

    # 4. Summary
    configured = ctx.db.providers_with_keys()
    first_provider = configured[0] if configured else None
    default_model = ctx.prefs.global_default() or "llama-3.3-70b-versatile"

    curl_examples = ""
    if first_provider:
        curl_examples = (
            f"\n"
            f"[bold]Try it:[/]\n"
            f"  curl http://localhost:8080/v1/chat/completions \\\n"
            f"    -H 'Authorization: Bearer YOUR_SYNCKEY_KEY' \\\n"
            f"    -H 'Content-Type: application/json' \\\n"
            f"    -d '{{\"model\": \"{default_model}\", \"messages\": [{{\"role\": \"user\", \"content\": \"Hi\"}}]}}'\n\n"
            f"[bold]With a specific provider:[/]\n"
            f"  curl http://localhost:8080/v1/chat/completions \\\n"
            f"    -H 'Authorization: Bearer YOUR_SYNCKEY_KEY' \\\n"
            f"    -H 'Content-Type: application/json' \\\n"
            f"    -d '{{\"model\": \"{first_provider}/{default_model}\", \"messages\": [{{\"role\": \"user\", \"content\": \"Hi\"}}]}}'\n"
        )

    console.print(
        Panel.fit(
            f"providers:      {', '.join(configured) or '(none)'}\n"
            f"models indexed: {len(ctx.router.index)}\n"
            f"global default: {default_model}\n\n"
            f"Start the gateway:  [bold]synckey serve[/]\n"
            f"Health-check keys:  [bold]synckey test[/]\n"
            f"Watch it live:      [bold]synckey dash[/]"
            f"{curl_examples}",
            title="Setup complete",
            border_style="green",
        )
    )
    ctx.close()


def _run_guide():
    """Interactive walkthrough of synckey concepts."""
    console.print(Panel.fit(
        "[bold]synckey guide[/] let's walk through how it all works.\n"
        "Press Enter at each step, or Ctrl+C to quit anytime.",
        border_style="cyan",
    ))
    time.sleep(0.5)

    console.print("\n[bold cyan]1. The unified key[/]")
    console.print("   synckey gives you ONE key that works with ALL providers.")
    console.print("   Your app uses this key synckey routes to the right provider.")
    input("\n   Press Enter to continue...")

    console.print("\n[bold cyan]2. How routing works[/]")
    console.print("   Send a model name → synckey finds which provider has it.")
    console.print("   Send 'provider/model' → forces that provider.")
    console.print("   Send nothing → uses your global default model.")
    console.print("   Send only provider → uses that provider's default model.")
    input("\n   Press Enter to continue...")

    console.print("\n[bold cyan]3. The fallback chain[/]")
    console.print("   Every model has a quality tier: FRONTIER > HIGH > MID > LOW")
    console.print("   If your first choice is rate-limited, synckey finds another at")
    console.print("   the SAME tier (never drops to a cheaper tier).")
    console.print("   Override per request: -H 'X-Quality-Floor: high'")
    input("\n   Press Enter to continue...")

    console.print("\n[bold cyan]4. When all keys are cooling[/]")
    console.print("   Instead of failing, synckey QUEUES your request and gives you")
    console.print("   a poll URL. It runs automatically when a key frees up.")
    console.print("   GET /v1/requests/<id> to check the result.")
    input("\n   Press Enter to continue...")

    console.print("\n[bold cyan]5. Routing presets[/]")
    ctx = load_ctx()
    preset = "custom"
    if ctx.settings.tier_fallback_enabled and ctx.settings.deep_cooling_threshold == 60.0:
        preset = "reliable"
    elif not ctx.settings.tier_fallback_enabled:
        preset = "cheap"
    console.print(f"   Current preset: [cyan]{preset}[/]")
    console.print("   reliable = tier fallback on, deep_cooling_threshold=60s")
    console.print("   cheap    = tier fallback off (prefer cost over quality)")
    console.print("   Run `synckey routing preset reliable` or `synckey routing preset cheap`")
    ctx.close()
    input("\n   Press Enter to continue...")

    console.print("\n[bold cyan]6. Try it[/]")
    console.print("   synckey serve          start the gateway")
    console.print("   synckey dash           live dashboard")
    console.print("   synckey test groq llama-3.3-70b-versatile 5  test a route with 5 calls")
    console.print("   synckey models        see which models are available")
    console.print("")
    console.print(Panel.fit(
        "[bold green]That's it![/] Run `synckey --help` anytime for the full command reference.",
        border_style="green",
    ))


@routing_app.command("priority")
def routing_priority(
    providers: str = typer.Argument(None, help="Space-separated provider list, e.g. groq gemini."),
):
    """Show or set provider priority order (first = preferred when multiple have the model)."""
    ctx = load_ctx()
    if providers:
        plist = [p.strip().lower() for p in providers.split() if p.strip()]
        for p in plist:
            if p not in ctx.providers:
                err_con.print(f"[red]Unknown provider:[/] {p}")
                err_con.print(f"[dim]Run `synckey providers` for the list.[/]")
                ctx.close()
                raise typer.Exit(1)
        ctx.settings.provider_priority = plist
        ctx.settings.save()
        console.print(f"[green]Priority ->[/] {' > '.join(plist)}")
    else:
        current = ctx.settings.provider_priority
        all_providers = sorted(ctx.providers.keys())
        if current:
            console.print(f"[cyan]Current priority:[/] {' > '.join(current)}")
        else:
            console.print("[dim]No priority set (uses provider order from config or default).[/]")
        console.print(f"[dim]All providers:[/] {', '.join(all_providers)}")
    ctx.close()


@routing_app.command("preset")
def routing_preset(
    name: str = typer.Argument(None, help="reliable | cheap | (empty to show current)"),
):
    """Set or show the routing fallback preset.

    reliable: tier fallback ON, deep_cooling_threshold=60s
              Never drops below the requested model's tier.
              Jumps to same-tier alternative after 60s of cooling.

    cheap:   tier fallback OFF
              Uses whatever key is available, regardless of tier.
              Can fall from FRONTIER to LOW if that's what's cheap/available.
    """
    ctx = load_ctx()
    if name is None:
        tfe = ctx.settings.tier_fallback_enabled
        dct = ctx.settings.deep_cooling_threshold
        if tfe and dct == 60.0:
            console.print(f"[cyan]Current preset:[/] [green]reliable[/]")
            console.print("  tier_fallback: on")
            console.print("  deep_cooling_threshold: 60s")
        elif not tfe:
            console.print(f"[cyan]Current preset:[/] [yellow]cheap[/]")
            console.print("  tier_fallback: off")
            console.print("  deep_cooling_threshold: 60s")
        else:
            console.print(f"[cyan]Current preset:[/] [dim]custom[/]")
            console.print(f"  tier_fallback: {tfe}")
            console.print(f"  deep_cooling_threshold: {dct}s")
        console.print("\n[dim]Set: synckey routing preset reliable | cheap[/]")
        ctx.close()
        return

    name = name.lower().strip()
    if name not in ("reliable", "cheap"):
        err_con.print("[red]Preset must be:[/] reliable | cheap")
        raise typer.Exit(1)

    if name == "reliable":
        ctx.settings.tier_fallback_enabled = True
        ctx.settings.deep_cooling_threshold = 60.0
    else:  # cheap
        ctx.settings.tier_fallback_enabled = False
        ctx.settings.deep_cooling_threshold = 60.0

    ctx.settings.save()
    console.print(f"[green]Preset set to:[/] [bold]{name}[/]")
    ctx.close()


@app.command()
def serve(
    host: str = typer.Option(None, "--host", "-h"),
    port: int = typer.Option(None, "--port", "-p"),
):
    """Run the unified OpenAI-compatible gateway (background)."""
    import subprocess
    import sys
    import os

    ctx = load_ctx()
    if not ctx.unified_key_hash():
        err_con.print("[red]Not initialized.[/] Run `synckey setup` first.")
        raise typer.Exit(1)
    if not ctx.db.providers_with_keys():
        err_con.print("[yellow]Warning:[/] no provider keys stored yet. Run `synckey setup` to add one.")

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
            f"default model:    {ctx.prefs.global_default() or '(none clients must send model)'}\n"
            f"max_retries:      {ctx.settings.max_retries}\n"
            f"tier fallback:    {'on' if ctx.settings.tier_fallback_enabled else 'off'}\n"
            f"deferred queue:   {'on' if ctx.settings.deferred_enabled else 'off'}"
            f" (202 + poll when all keys cooling; TTL {int(ctx.settings.deferred_ttl)}s)\n"
            f"quality floor:    per-request via X-Quality-Floor header",
            border_style="green",
        )
    )
    ctx.close()

    srv = os.path.join(os.path.dirname(__file__), "_serve_temp.py")
    with open(srv, "w") as f:
        f.write("import uvicorn\n")
        f.write("from synckey.server import create_app\n")
        f.write("from synckey.context import Context\n")
        f.write(f"uvicorn.run(create_app(Context()), host='{bind_host}', port={bind_port}, log_level='warning', access_log=False)\n")

    kwargs = {"cwd": os.getcwd()}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL

    subprocess.Popen([sys.executable, srv], **kwargs)
    console.print("\n[green]Gateway running in background.[/] Use [bold]synckey status[/] to monitor.")


@app.command()
def usage(
    hours: float = typer.Option(None, "--hours"),
    recent: bool = typer.Option(False, "--recent"),
    spend: bool = typer.Option(False, "--spend", help="Show cost breakdown by provider and model."),
):
    """Token usage and request monitoring.

    Use --spend for cost breakdown, --recent for last 25 requests.
    """
    ctx = load_ctx()
    since = time.time() - hours * 3600 if hours else None
    totals = ctx.db.usage_totals(since)

    if spend:
        total_cost = totals["cost_usd"] or 0.0
        console.print(f"[bold]Total spend:[/] [green]{fmt_cost(total_cost)}[/]"
                      + (f" (last {hours}h)" if hours else " (all time)"))
        summary = ctx.db.usage_summary(since)
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
        return

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

    console.print(
        Panel.fit(
            f"requests:  [bold]{totals['requests'] or 0}[/]  ([red]{totals['errors'] or 0} errors[/])\n"
            f"tokens:    [bold cyan]{fmt_num(totals['total_tokens'])}[/] total "
            f"({fmt_num(totals['prompt_tokens'])} prompt + {fmt_num(totals['completion_tokens'])} completion)\n"
            f"cost:      [bold green]{fmt_cost(totals['cost_usd'])}[/]",
            title=f"Usage{' (last {}h)'.format(hours) if hours else ' (all time)'}",
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


@app.command("spend", hidden=True)
def spend(hours: float = typer.Option(None, "--hours")):
    """Cost breakdown by provider and model. (Use: synckey usage --spend)"""
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
            f"initialized:  {'[green]yes[/]' if initialized else '[red]no, run synckey setup[/]'}\n"
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
def test(
    provider: str = typer.Argument(None, help="Provider id (omit to health-check all)."),
    model: str = typer.Argument(None, help="Model id (with provider: makes actual test calls)."),
    calls: int = typer.Argument(1, help="Number of test calls to make."),
):
    """Health-check stored keys, or test a route with actual calls.

    Without model: hits each provider's /models endpoint (health check).

    With provider + model: makes `calls` actual API calls to that provider/model
    and shows per-call routing, latency, tokens, and status.
    """
    import httpx

    ctx = load_ctx()

    if model:
        _run_test_calls(ctx, provider, model, calls)
        ctx.close()
        return

    # Health-check mode (original behavior)
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
            try:
                secret = ctx.box.open(k.secret)
            except RuntimeError:
                table.add_row(pid, f"#{k.id} {k.label}", cur, "[red]key corrupt re-add with synckey setup[/]")
                continue
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


async def _call_model(ctx, provider, model, k, secret):
    """Make a single test call; returns (status_code, latency_ms, tokens, error, chain)."""
    import httpx
    prov = ctx.providers.get(provider)
    if not prov:
        return 0, 0, 0, f"unknown provider {provider}", ""

    body = {
        "model": model,
        "messages": [{"role": "user", "content": "say hi in one word"}],
        "max_tokens": 10,
    }
    chain_parts = [f"{provider}/key#{k.id}"]
    try:
        url = prov.base_url.rstrip("/") + "/chat/completions"
        t0 = time.time()
        resp = httpx.AsyncClient(timeout=30.0).post(
            url,
            headers={**prov.auth_headers(secret), "Content-Type": "application/json"},
            json=body,
        )
        resp = await resp
        latency = int((time.time() - t0) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            tokens = (data.get("usage") or {}).get("total_tokens", 0)
            return resp.status_code, latency, tokens, None, " > ".join(chain_parts)
        else:
            err_msg = f"HTTP {resp.status_code}"
            try:
                err_msg = resp.json().get("error", {}).get("message", err_msg)
            except Exception:
                pass
            return resp.status_code, latency, 0, err_msg, " > ".join(chain_parts)
    except Exception as exc:
        return 0, 0, 0, str(exc), " > ".join(chain_parts)


def _run_test_calls(ctx, provider, model, calls):
    """Run `calls` actual test calls to the given provider/model."""
    if provider.lower() not in ctx.providers:
        err_con.print(f"[red]Unknown provider:[/] {provider}")
        err_con.print(f"[dim]Run `synckey providers` for the list.[/]")
        return

    keys = ctx.db.list_keys(provider=provider.lower(), enabled_only=True)
    if not keys:
        err_con.print(f"[red]No enabled keys for[/] {provider}.")
        err_con.print(f"[dim]Run `synckey setup` first.[/]")
        return

    console.print(f"[cyan]Testing[/] {provider}/{model} {calls} call(s) with {len(keys)} key(s)\n")

    table = Table(title=f"Route test: {provider}/{model}")
    table.add_column("#", justify="right")
    table.add_column("key")
    table.add_column("provider", style="cyan")
    table.add_column("model")
    table.add_column("status", style="bold")
    table.add_column("ms", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("error / chain")

    import asyncio
    results = []
    for i in range(calls):
        for k in keys:
            try:
                secret = ctx.box.open(k.secret)
            except RuntimeError:
                err_con.print(f"[red]Key #{k.id} is corrupt (secret.key may have changed).[/]")
                err_con.print(f"[dim]Re-add it: synckey setup[/]")
                continue
            status, latency, tokens, error, chain = asyncio.run(
                _call_model(ctx, provider.lower(), model, k, secret)
            )
            results.append((i + 1, k, provider, model, status, latency, tokens, error, chain))

    for i, k, prov, mod, status, latency, tokens, error, chain in results:
        status_str = f"[green]{status}[/]" if status == 200 else f"[red]{status}[/]"
        err_str = f"[red]{error}[/]" if error else chain
        table.add_row(
            str(i), f"#{k.id}", prov, mod,
            status_str, str(latency), str(tokens) if tokens else "—", err_str,
        )
    console.print(table)


def _read_key(timeout: float) -> str | None:
    """Poll for a single keypress for up to `timeout` seconds. Windows: msvcrt.

    Returns the lowercased character, or None if nothing was pressed (or the
    platform has no non-blocking console read then it just sleeps).
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

    Keys: [1] overview  [2] keys  [3] events  [4] routing  [p] pause  [r] refresh  [q] quit.
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
            "[bold]4[/] routing   [bold]p[/] pause   [bold]r[/] refresh   [bold]q[/] quit"
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

    def _routing_panel() -> Panel:
        chains = ctx.db.recent_routing_chains(15)
        if not chains:
            return Panel(
                "[dim]No routing chain data yet.\n"
                "Send requests through the gateway to see the routing path here.\n"
                "Or run: synckey test groq llama-3.3-70b-versatile 5[/]",
                title="Routing chain",
                border_style="cyan",
            )
        t = Table(show_header=True, header_style="bold", expand=True, box=None)
        for col in ("when", "model", "provider", "key", "chain"):
            t.add_column(col)
        for r in chains:
            ago = int(time.time() - r["ts"])
            chain_text = r["chain"] or ""
            t.add_row(
                f"{ago}s",
                r["model"] or "",
                r["provider"] or "",
                f"#{r['key_id']}" if r["key_id"] else "",
                chain_text[:80] + ("…" if len(chain_text) > 80 else ""),
            )
        return Panel(t, title="Routing chain (recent)")

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
        elif view == "routing":
            layout["body"].update(_routing_panel())
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
    views = {"1": "overview", "2": "keys", "3": "events", "4": "routing"}
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
def renew():
    """Regenerate your unified API key (old key becomes invalid)."""
    ensure_home()
    ctx = Context()
    unified = generate_unified_key()
    ctx.db.set_meta("unified_key_hash", sha256(unified))
    ctx.db.set_meta("created_at", str(time.time()))
    _ = ctx.box
    console.print(
        Panel.fit(
            f"Your new unified API key (old key is now invalid):\n\n  [bold cyan]{unified}[/]\n\n"
            f"Update your clients to use the new key.",
            title="Key regenerated",
            border_style="green",
        )
    )
    ctx.close()


@app.command()
def version():
    """Print the synckey version."""
    console.print(f"synckey {__version__}")


if __name__ == "__main__":
    app()
