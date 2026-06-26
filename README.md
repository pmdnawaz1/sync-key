# synckey

**Merge every AI provider key behind one unified, OpenAI-compatible API key.**

Gemini, Groq, NVIDIA, GitHub Models, Cerebras, Cohere, Mistral, DeepSeek, xAI,
OpenAI, OpenRouter, Together, SambaNova, Ollama — and any custom OpenAI-compatible
provider — all behind a single key you point your OpenAI SDK at.

No frontend. No Redis. Just a CLI and a fast local gateway.

```
                         ┌──────────────────────────────────────────┐
  your app               │           synckey gateway                │
  (OpenAI SDK) ──────────▶  auth → route → key pool → failover     │──▶ Groq
  base_url=:8787/v1      │         ▲           │                    │──▶ Gemini
  api_key=sk-synckey-... │         │           ▼                    │──▶ Cerebras
                         │   model index   token metering (SQLite)  │──▶ Cohere …
                         └──────────────────────────────────────────┘
```

---

## First 5 minutes

```bash
py -m venv .venv && .venv\Scripts\activate && pip install -e .

synckey setup     # guided: mint key → add provider keys → fetch models → set defaults
synckey serve     # start the gateway
```

`synckey setup` walks you through everything interactively. It mints your unified
key (shown once — store it), lets you paste keys for each provider (single,
comma-separated, or a file), offers to fetch the models those keys can actually
call, and asks for a **default model** so clients can stay simple.

Then point any OpenAI client at it:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    api_key="sk-synckey-...",   # your unified key from setup
)

# The simplest call: no model needed if you set a default.
client.chat.completions.create(messages=[{"role": "user", "content": "hi"}])

# Or name a model — auto-routed to the right provider.
client.chat.completions.create(model="gemini-2.0-flash", messages=[...])
```

That's it. Everything below is optional refinement.

---

## The two things that confused you, settled

### 1. How does the client choose a model?

You can send as much or as little as you want. The rule is **the model wins; the
provider is a hint.**

| What the client sends | What synckey does |
|---|---|
| `model: "gemini-2.0-flash"` | Routes by model (live index → `provider/` prefix → name pattern). |
| `model: "groq/llama-3.3-70b"` | Explicit `provider/model` prefix — forced to that provider. |
| `model` + `provider: "groq"` | Routes by **model**; the `provider` only moves Groq to the front of the candidate list (ignored if Groq can't serve it). |
| `model: "default"` *or* model omitted | Uses the **global default model**. |
| `model` omitted + `provider: "groq"` | Uses **Groq's default model**. |
| `model: "fast"` (an alias) | Expands the alias, then routes. |

Plus any normal OpenAI fields (`temperature`, `max_tokens`, `stream`, …) pass
straight through. `provider` is a synckey-only hint and is stripped before the
request reaches the upstream.

```python
# All valid:
client.chat.completions.create(messages=[...])                                   # global default
client.chat.completions.create(model="default", extra_body={"provider": "groq"}) # groq's default
client.chat.completions.create(model="fast", messages=[...], temperature=0.2)    # alias
client.chat.completions.create(model="claude-opus-4-8", messages=[...])          # explicit
```

```bash
# Header form of the provider hint, and the per-request quality floor:
curl :8787/v1/chat/completions -H "Authorization: Bearer sk-synckey-..." \
  -H "X-Provider: groq" -H "X-Quality-Floor: high" \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

### 2. How are defaults, tiers, and aliases configured?

All of it via the CLI — you never hand-edit a file.

```bash
# Defaults (what runs when the client doesn't specify a model)
synckey config set-default gemini-2.0-flash                 # global default
synckey config provider-default groq llama-3.3-70b-versatile  # per-provider default

# Aliases (friendly names clients can send as the model)
synckey config alias fast mid          # "fast" → best live MID-tier model
synckey config alias smart frontier    # "smart" → best live FRONTIER model
synckey config alias cheap gemini-1.5-flash

# Tiers (auto-detected; override only when synckey guesses wrong)
synckey tier set my-fine-tuned-model frontier
synckey tier list

# See everything in one place
synckey config
```

A default (global or per-provider) can be a **concrete model id** (`gemini-2.0-flash`),
a **tier** (`mid`), or an **alias** (`fast`). Tiers and aliases are resolved to a
live model at request time.

---

## Configuring keys

```bash
# Single key (prompted, hidden)
synckey key add groq

# Many keys at once — comma-separated or from a file
synckey key add groq --key "sk-1,sk-2,sk-3"
synckey key add groq --file ~/groq-keys.txt     # one per line and/or comma-separated
synckey key add groq --from-env                 # reads GROQ_API_KEY etc.

# Skip the model-fetch prompt and set the default inline (good for scripts)
synckey key add groq --key "sk-1,sk-2" --no-fetch --default llama-3.3-70b-versatile
```

After adding, `key add` offers to **fetch the models those keys can call**. If two
keys for the same provider expose different model sets, it shows you the per-key
difference instead of a silently merged list. Then it offers to set that
provider's default model.

| Command | What it does |
|---|---|
| `synckey key add <provider>` | Store credential(s): single, `--key a,b,c`, or `--file`. Then fetch + default. |
| `synckey key list [provider]` | Health matrix: status, RPM cap/used, requests, errors, cost. |
| `synckey key rm <id>` | Delete a key. |
| `synckey key enable/disable <id>` | Flip a key on/off without deleting it. |
| `synckey key limits <id>` | Set `--rpm` / `--tpm` caps that drive the proactive bucket. |

---

## How routing works

1. **Explicit prefix** — `provider/model` when the prefix is a known provider id
   (`groq/…`, `cohere/…`). Org-namespaced names like `meta/llama-3.3-70b` are left intact.
2. **Live model index** — providers whose `/models` endpoint advertises the model,
   ordered by your configured `priority`. Authoritative.
3. **Name patterns** — `gemini-*` → Gemini, `command-*` → Cohere, `claude-*` → Anthropic.

Within each provider, every live key is tried before moving to the next provider.
A `provider` hint reorders these candidates but never overrides an explicit model.

## Model quality tiers and smart fallback

synckey detects each model's quality tier automatically:

| Tier | Examples |
|------|---------|
| FRONTIER | claude-opus-4-8, gpt-5, gemini-2.5-pro |
| HIGH | claude-sonnet-4-6, gpt-4o, gemini-1.5-pro |
| MID | gpt-4o-mini, llama-3.3-70b, gemini-2.0-flash |
| LOW | gemma-2b, llama-3.2-1b |

**The floor rule:** fallback chains never drop below the requested model's tier. If
you call Claude Opus (FRONTIER), synckey will try other FRONTIER models (GPT-5,
Gemini 2.5 Pro) but never fall to a MID OSS model.

**Override the floor per request:** `-H "X-Quality-Floor: high"`.

**Override a tier permanently:** `synckey tier set <model> <tier>`.

**Smart cooling skip:** if all keys for the primary model are cooling for > 60 s,
synckey immediately routes to a same-tier alternative on another provider rather
than waiting behind the cooldown.

## Deferred requests (when every key is rate-limited)

When no key has spare capacity for a request — every candidate is either
provider-cooling (already 429'd) or locally bucket-throttled (the proactive
limiter is holding it back) — synckey doesn't just 503. It **queues the request**
and hands back a poll id. A background worker replays it through the normal
keypool the moment a key has room, stores the response, and serves it on a later
GET. The result is purged `ttl` seconds after it completes.

```jsonc
// POST /v1/chat/completions while everything is cooling → 202 Accepted
{
  "id": "defer_AbC123…",
  "object": "deferred",
  "status": "queued",
  "model": "llama-3.3-70b-versatile",
  "retry_after": 28,                       // also sent as a Retry-After header
  "result_url": "/v1/requests/defer_AbC123…"
}
```

Poll it after `retry_after` seconds with your unified key:

```bash
curl :8787/v1/requests/defer_AbC123... -H "Authorization: Bearer sk-synckey-..."
# → 202 + new ETA while still queued/running
# → 200 + the actual completion once a key ran it
# → 404 once it has been purged (TTL expired)
```

Notes:
- **Automatic** — any caller gets a 202 when keys are exhausted. (A standard
  OpenAI SDK call will surface the 202 as an error; deferral-aware clients poll
  `result_url`.) Turn it off with `[deferred].enabled = false` to get the old 503.
- Deferred requests run **non-streaming**; the stored result is a complete body.
- The queue is **durable** (SQLite): a gateway restart resumes pending jobs and
  still serves stored responses.

```bash
synckey deferred list          # queued / running / done, with ETAs
synckey deferred get <id>      # status + stored response
```

```toml
[deferred]
enabled = true
ttl = 3600          # keep a finished response this long (seconds), then purge
poll = 5            # worker scan interval
max_queue = 1000    # reject new deferrals beyond this many queued
max_queue_age = 86400   # drop a job that never ran within this window
```

## Durable key health

Key states persist to SQLite on every transition:

- **LIVE** — healthy, bucket has capacity.
- **COOLING** — got 429'd; cooldown tracks `Retry-After`. Auto-recovers across restarts.
- **DEAD** — got 401/403; excluded until `synckey key enable <id>`.

```bash
synckey key list          # health, cooldown remaining, dead reason
synckey key enable <id>   # revive a dead key after you fix it
```

## Live dashboard

```bash
synckey dash              # refresh every 2 seconds
synckey dash -i 5         # every 5 seconds
```

Interactive keys: **1** overview · **2** keys · **3** events · **p** pause ·
**r** refresh · **q** quit. Shows totals, per-key health/burn rate, routing
events, and recent requests.

## Gateway endpoints

All OpenAI-compatible:

| Endpoint | Notes |
|---|---|
| `POST /v1/chat/completions` | Streaming and non-streaming. Optional `provider` hint, `model` optional if a default is set. |
| `POST /v1/embeddings` | Non-streaming. |
| `POST /v1/completions` | Legacy completions. |
| `GET  /v1/requests/{id}` | Poll a deferred request (see below). |
| `GET  /v1/models` | Aggregated across providers; includes `synckey_tier`. |
| `GET  /v1/usage` | Live token and cost totals (synckey extension). |
| `GET  /v1/events?limit=50` | Recent routing events (synckey extension). |
| `GET  /healthz` | Key counts, provider list, model count. |

---

## Advanced

### Where settings live

Two stores, by intent:

- **Set via CLI (DB):** global default, per-provider defaults, aliases, tier
  overrides. These are the things you tune as you go — manage them with
  `synckey config` and `synckey tier`.
- **Hand-edited (`~/.synckey/config.toml`):** static infra you version-control.

```toml
[gateway]
host = "127.0.0.1"   # 0.0.0.0 to expose (put nginx/caddy in front)
port = 8787
request_timeout = 120

[routing]
priority = ["cerebras", "groq", "nvidia"]   # tie-break provider order
cross_provider_fallback = true
max_retries = 6
default_cooldown = 20         # seconds when no Retry-After header
deep_cooling_threshold = 60   # skip to tier alts when all primary keys cooling > this

[tiers]
fallback = true
overrides = {"my-model" = "frontier"}       # same effect as `synckey tier set`
prices = {"my-model" = [1.0, 3.0]}          # [input $/M, output $/M]

# Add any OpenAI-compatible provider not built in:
[[providers]]
id = "fireworks"
name = "Fireworks AI"
base_url = "https://api.fireworks.ai/inference/v1"
env = ["FIREWORKS_API_KEY"]
patterns = ["^accounts/fireworks/"]
```

State lives under `~/.synckey/` (override with `$SYNCKEY_HOME`):
- `secret.key` — Fernet key sealing provider credentials (0600 perms)
- `synckey.db` — SQLite: keys, usage, events, key states, defaults/aliases
- `config.toml` — static settings and custom providers

> CLI changes to defaults/aliases/tiers apply on the next `synckey serve`.

### Custom domain / reverse proxy

The gateway binds to `127.0.0.1` by default.

**nginx:**

```nginx
server {
    listen 443 ssl;
    server_name ai.example.com;
    ssl_certificate     /etc/ssl/certs/ai.example.com.crt;
    ssl_certificate_key /etc/ssl/private/ai.example.com.key;
    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_buffering off;           # required for streaming
        proxy_read_timeout 120s;
    }
}
```

**Caddy:**

```caddyfile
ai.example.com {
    reverse_proxy localhost:8787 {
        flush_interval -1   # disable buffering for streaming
    }
}
```

```bash
synckey serve --host 0.0.0.0 --port 8787   # or set host in config.toml
```

### Analytics

```bash
synckey usage                     # lifetime totals
synckey usage --hours 24          # last 24 hours
synckey usage --recent            # last 25 requests with status/latency
synckey spend                     # cost breakdown by model
synckey events                    # routing events
synckey events --type key_dead    # filter: rate_limited | key_dead | tier_fallback
```

Via HTTP (unified key required):

```bash
curl :8787/v1/usage  -H "Authorization: Bearer sk-synckey-..."
curl :8787/v1/events -H "Authorization: Bearer sk-synckey-..."
curl :8787/healthz
```

### Performance

Hot path: key state (cooldowns, buckets) lives entirely in memory — no DB read
per request; secrets decrypted once and cached; usage rows handed to a background
writer; responses streamed straight through; async end to end (uvicorn + httpx).

Measured on a single pinned core, 200-way concurrency, 20k requests against a
local upstream: **~270 req/s, 0 errors, ~83 MB RSS**.

### Security

- Provider secrets are Fernet-encrypted at rest; only the last 4 chars are shown.
- The unified key is stored as a SHA-256 hash — keep the plaintext from setup safe.
- The gateway binds to `127.0.0.1` by default. Only expose deliberately.
- `secret.key` is 0600; `~/.synckey/` is 0700.

---

## Full CLI reference

```
Getting started
  synckey setup                 guided first-run
  synckey init                  mint the unified key only
  synckey key add <provider>    store credential(s) + fetch models + set default
  synckey serve [--host --port] run the gateway

Configuration
  synckey config                          show defaults, aliases, settings
  synckey config set-default <model>      global default model
  synckey config provider-default <p> <m> per-provider default
  synckey config alias <name> <target>    define a client-facing alias
  synckey config unalias <name>           remove an alias
  synckey tier set <model> <tier>         override a model's tier
  synckey tier unset <model>              revert to auto-detected tier
  synckey tier list                       show tier overrides

Keys & models
  synckey providers                       list supported providers
  synckey key list|rm|enable|disable|limits
  synckey models [--refresh] [-p prov] [--tier ...]
  synckey detect <model>                  show routing, tier, floor, price

Monitoring
  synckey dash                            live dashboard (1/2/3, p/r/q)
  synckey usage [--hours N] [--recent]
  synckey spend [--hours N]
  synckey events [--type] [-n limit]
  synckey deferred list|get               inspect the deferred queue
  synckey status
  synckey test [provider]                 health-check stored keys
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
