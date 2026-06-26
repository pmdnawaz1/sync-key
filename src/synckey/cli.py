"""synckey command-line interface.

    synckey init                 one-time setup; prints your unified key
    synckey key add <provider>   store a provider credential (repeatable)
    synckey key list|rm|enable   manage stored credentials
    synckey providers            list every supported provider
    synckey models [--refresh]   discover models your keys can call
    synckey detect <model>       show how a model name routes
    synckey serve                run the unified gateway
    synckey usage                token and request monitoring
    synckey status               at-a-glance overview
    synckey test [provider]      health-check stored keys
"""

from __future__ import annotations

import asyncio
import os
import time

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import ensure_home, generate_unified_key, home
from .context import Context
from .db import sha256

app = typer.Typer(
    name="synckey",
    help="Merge every AI provider key behind one unified, OpenAI-compatible API key.",
    no_args_is_help=True,
    add_completion=False,
)
key_app = typer.Typer(help="Manage stored provider credentials.", no_args_is_help=True)
app.add_typer(key_app, name="key")

console = Console()
err = Console(stderr=True)


def load_context() -> Context:
    ensure_home()
    return Context()


def format_count(n: int | None) -> str:
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


@app.command()
def init(force: bool = typer.Option(False, "--force", help="Regenerate the unified key.")):
    """Initialize synckey and mint your unified API key."""
    ensure_home()
    ctx = Context()
    if ctx.unified_key_hash() and not force:
        err.print("[yellow]Already initialized.[/] Use --force to mint a new unified key.")
        console.print(f"Config home: [cyan]{home()}[/]")
        raise typer.Exit(0)

    unified = generate_unified_key()
    ctx.db.set_meta("unified_key_hash", sha256(unified))
    ctx.db.set_meta("created_at", str(time.time()))
    _ = ctx.box  # ensure secret.key exists with correct perms

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
    ctx = load_context()
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


@key_app.command("add")
def key_add(
    provider: str = typer.Argument(..., help="Provider id, e.g. groq, gemini, cohere."),
    key: str = typer.Option(None, "--key", "-k", help="The secret. Omit to be prompted."),
    label: str = typer.Option(None, "--label", "-l", help="A name to tell keys apart."),
    weight: int = typer.Option(1, "--weight", "-w", help="Round-robin weight (higher is more)."),
    from_env: bool = typer.Option(False, "--from-env", help="Read the key from the provider env var."),
):
    """Store a provider credential. Add several per provider for round-robin."""
    ctx = load_context()
    provider = provider.lower()
    if provider not in ctx.providers:
        err.print(f"[red]Unknown provider '{provider}'.[/] Run `synckey providers`.")
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
            err.print(f"[red]No key found in env vars:[/] {', '.join(prov.env) or '(none)'}")
            raise typer.Exit(1)
    if not secret:
        secret = typer.prompt(f"{prov.name} API key", hide_input=True)
    secret = secret.strip()
    if not secret:
        err.print("[red]Empty key.[/]")
        raise typer.Exit(1)

    existing = len(ctx.db.list_keys(provider=provider))
    label = label or f"{provider}-{existing + 1}"
    key_id = ctx.db.add_key(provider, label, ctx.box.seal(secret), weight=weight)
    masked = secret[-4:] if len(secret) >= 4 else "***"
    console.print(
        f"[green]Added[/] {prov.name} key [cyan]#{key_id}[/] "
        f"(label: {label}, weight: {weight}). Masked: ...{masked}"
    )
    ctx.close()


@key_app.command("list")
def key_list(provider: str = typer.Argument(None, help="Filter by provider id.")):
    """List stored credentials (secrets stay masked)."""
    ctx = load_context()
    keys = ctx.db.list_keys(provider=provider.lower() if provider else None)
    if not keys:
        console.print("[yellow]No keys stored.[/] Add one: `synckey key add <provider>`")
        ctx.close()
        return
    counts = ctx.db.key_request_counts()
    table = Table(title="Stored keys")
    table.add_column("id", style="cyan", justify="right")
    table.add_column("provider")
    table.add_column("label")
    table.add_column("weight", justify="right")
    table.add_column("enabled")
    table.add_column("requests", justify="right")
    table.add_column("errors", justify="right")
    for k in keys:
        reqs, errs = counts.get(k.id, (0, 0))
        table.add_row(
            str(k.id),
            k.provider,
            k.label,
            str(k.weight),
            "yes" if k.enabled else "[dim]no[/]",
            str(reqs),
            f"[red]{errs}[/]" if errs else "0",
        )
    console.print(table)
    ctx.close()


@key_app.command("rm")
def key_remove(key_id: int = typer.Argument(..., help="Key id from `key list`.")):
    """Remove a stored credential."""
    ctx = load_context()
    if ctx.db.remove_key(key_id):
        console.print(f"[green]Removed[/] key #{key_id}.")
    else:
        err.print(f"[red]No key #{key_id}.[/]")
    ctx.close()


@key_app.command("enable")
def key_enable(key_id: int = typer.Argument(...)):
    """Re-enable a disabled key."""
    ctx = load_context()
    ctx.db.set_key_enabled(key_id, True)
    console.print(f"[green]Enabled[/] key #{key_id}.")
    ctx.close()


@key_app.command("disable")
def key_disable(key_id: int = typer.Argument(...)):
    """Disable a key without deleting it."""
    ctx = load_context()
    ctx.db.set_key_enabled(key_id, False)
    console.print(f"[yellow]Disabled[/] key #{key_id}.")
    ctx.close()


@app.command()
def models(
    provider: str = typer.Option(None, "--provider", "-p", help="Filter to one provider."),
    refresh: bool = typer.Option(False, "--refresh", "-r", help="Re-query providers live."),
):
    """List the models your configured keys can call."""
    ctx = load_context()
    if refresh or not ctx.router.index:
        console.print("[dim]Querying providers for available models...[/]")
        discovered = asyncio.run(ctx.router.refresh_index(ctx.first_secret))
        for pid, ms in discovered.items():
            color = "green" if ms else "red"
            console.print(f"  [{color}]{pid}[/]: {len(ms)} models")

    index = ctx.router.index
    if not index:
        console.print("[yellow]No models discovered.[/] Add keys then run `synckey models -r`.")
        ctx.close()
        return

    rows = [
        (model, pid)
        for model, provs in sorted(index.items())
        for pid in sorted(provs)
        if not provider or pid == provider.lower()
    ]
    table = Table(title=f"Usable models ({len(rows)})")
    table.add_column("model", style="cyan")
    table.add_column("provider")
    for model, pid in rows:
        table.add_row(model, pid)
    console.print(table)
    console.print(f"[dim]Index age: {int(ctx.router.index_age())}s. Use --refresh to update.[/]")
    ctx.close()


@app.command()
def detect(model: str = typer.Argument(..., help="A model name to route.")):
    """Show which provider(s) a model name resolves to, and how."""
    ctx = load_context()
    res = ctx.router.resolve(model)
    if not res.providers:
        console.print(
            f"[yellow]'{model}' is unroutable.[/] No prefix, index entry, or pattern matched. "
            "Try `synckey models --refresh` or a 'provider/model' prefix."
        )
    else:
        console.print(
            Panel.fit(
                f"model:     [cyan]{model}[/]\n"
                f"upstream:  [cyan]{res.bare_model}[/]\n"
                f"routed by: [bold]{res.how}[/]\n"
                f"providers: {' > '.join(res.providers)} (tried in this order)",
                title="Routing",
                border_style="cyan",
            )
        )
    ctx.close()


@app.command()
def serve(
    host: str = typer.Option(None, "--host", "-h", help="Bind host (default from config)."),
    port: int = typer.Option(None, "--port", "-p", help="Bind port (default from config)."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload (dev)."),
):
    """Run the unified OpenAI-compatible gateway."""
    import uvicorn

    from .server import create_app

    ctx = load_context()
    if not ctx.unified_key_hash():
        err.print("[red]Not initialized.[/] Run `synckey init` first.")
        raise typer.Exit(1)
    if not ctx.db.providers_with_keys():
        err.print("[yellow]Warning:[/] no provider keys stored yet. Add one: `synckey key add`.")

    bind_host = host or ctx.settings.host
    bind_port = port or ctx.settings.port
    console.print(
        Panel.fit(
            f"[bold green]synckey gateway[/]\n\n"
            f"OpenAI base_url:  [cyan]http://{bind_host}:{bind_port}/v1[/]\n"
            f"providers:        {', '.join(ctx.db.providers_with_keys()) or '(none)'}\n"
            f"models indexed:   {len(ctx.router.index)}\n"
            f"rate-limit eater: on (max_retries={ctx.settings.max_retries})",
            border_style="green",
        )
    )
    uvicorn.run(
        create_app(ctx),
        host=bind_host,
        port=bind_port,
        reload=reload,
        log_level="info",
        access_log=False,
    )


@app.command()
def usage(
    hours: float = typer.Option(None, "--hours", help="Only count the last N hours."),
    recent: bool = typer.Option(False, "--recent", help="Show the latest requests instead."),
):
    """Token usage and request monitoring."""
    ctx = load_context()
    since = time.time() - hours * 3600 if hours else None

    if recent:
        table = Table(title="Recent requests")
        for col in ("when", "provider", "model", "tokens", "status", "ms"):
            table.add_column(col)
        for r in ctx.db.recent_usage(25):
            ago = int(time.time() - r["ts"])
            status = r["status_code"] or 0
            color = "green" if status == 200 else "red"
            table.add_row(
                f"{ago}s ago",
                r["provider"],
                r["model"],
                format_count(r["total_tokens"]),
                f"[{color}]{status}[/]",
                str(r["latency_ms"]),
            )
        console.print(table)
        ctx.close()
        return

    totals = ctx.db.usage_totals(since)
    console.print(
        Panel.fit(
            f"requests:  [bold]{totals['requests'] or 0}[/]  ([red]{totals['errors'] or 0} errors[/])\n"
            f"tokens:    [bold cyan]{format_count(totals['total_tokens'])}[/] total\n"
            f"           {format_count(totals['prompt_tokens'])} prompt + "
            f"{format_count(totals['completion_tokens'])} completion",
            title=f"Usage{f' (last {hours}h)' if hours else ' (all time)'}",
            border_style="cyan",
        )
    )

    summary = ctx.db.usage_summary(since)
    if summary:
        table = Table(title="By model")
        for col in ("provider", "model", "reqs", "tokens", "errors", "avg ms"):
            table.add_column(col)
        for r in summary:
            table.add_row(
                r["provider"],
                r["model"],
                str(r["requests"]),
                format_count(r["total_tokens"]),
                str(r["errors"] or 0),
                f"{r['avg_latency']:.0f}" if r["avg_latency"] else "",
            )
        console.print(table)
    ctx.close()


@app.command()
def status():
    """At-a-glance overview of your synckey setup."""
    ctx = load_context()
    initialized = bool(ctx.unified_key_hash())
    keys = ctx.db.list_keys()
    configured = ctx.db.providers_with_keys()
    totals = ctx.db.usage_totals()
    console.print(
        Panel.fit(
            f"home:         [cyan]{home()}[/]\n"
            f"initialized:  {'[green]yes[/]' if initialized else '[red]no, run synckey init[/]'}\n"
            f"providers:    {len(configured)} configured ({', '.join(configured) or 'none'})\n"
            f"keys:         {len(keys)} stored\n"
            f"models known: {len(ctx.router.index)}\n"
            f"lifetime:     {totals['requests'] or 0} requests, "
            f"{format_count(totals['total_tokens'])} tokens\n"
            f"gateway:      http://{ctx.settings.host}:{ctx.settings.port}/v1",
            title=f"synckey {__version__}",
            border_style="magenta",
        )
    )
    ctx.close()


@app.command()
def test(provider: str = typer.Argument(None, help="Test one provider, or all if omitted.")):
    """Health-check stored keys by hitting each provider's models endpoint."""
    import httpx

    ctx = load_context()
    targets = [provider.lower()] if provider else ctx.db.providers_with_keys()
    if not targets:
        console.print("[yellow]No providers to test.[/] Add a key first.")
        ctx.close()
        return

    table = Table(title="Key health")
    for col in ("provider", "key", "result"):
        table.add_column(col)

    for pid in targets:
        prov = ctx.providers.get(pid)
        if not prov:
            table.add_row(pid, "", "[red]unknown provider[/]")
            continue
        url = prov.base_url.rstrip("/") + prov.models_path
        for k in ctx.db.list_keys(provider=pid, enabled_only=True):
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
                    result = "[red]auth failed[/]"
                elif resp.status_code == 429:
                    result = "[yellow]rate limited[/]"
                else:
                    result = f"[red]HTTP {resp.status_code}[/]"
            except httpx.HTTPError as exc:
                result = f"[red]{type(exc).__name__}[/]"
            table.add_row(pid, f"#{k.id} {k.label}", result)
    console.print(table)
    ctx.close()


@app.command()
def version():
    """Print the synckey version."""
    console.print(f"synckey {__version__}")


if __name__ == "__main__":
    app()
