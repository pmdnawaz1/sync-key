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

## Features

- **One unified key** — `sk-synckey-…`. Your apps never see real provider keys.
- **Provider auto-detection** — `gemini-2.0-flash` goes to Gemini, `command-r` to Cohere, `groq/llama-3.3-70b-versatile` to Groq. Explicit prefix → live model index → name patterns.
- **Round-robin + weights** — multiple keys per provider; load spreads across them automatically.
- **Proactive rate limiting** — per-key token buckets pre-flight every request. The gateway stops sending to a key *before* it 429s, not after. Caps self-tune: they tighten on 429 feedback and relax after a success streak.
- **Smart cooling skip** — if all keys for a provider are cooling for > 60 s, the gateway immediately jumps to a same-tier alternative model on another provider instead of queuing behind the cooldown.
- **Model quality tiers** (FRONTIER / HIGH / MID / LOW) — fallback chains never degrade below the requested model's tier. Claude Opus won't fall to an OSS llama, but it will fall to GPT-5.
- **Durable key health** — key states survive process restarts. A key dead from a 401 stays dead; a key cooling for 20 min is still cooling after a restart. Nothing is lost from RAM.
- **Full analytics** — RPM/TPM burn rate per key, cost tracking ($/M tokens), tier fallback events, key death reasons, live dashboard.
- **Streaming** — SSE passthrough with usage counting.
- **Encrypted at rest** — provider secrets are Fernet-sealed; the DB never holds plaintext.

## Install

```bash
uv venv && uv pip install -e .
# or: pip install -e .
```

## Quickstart

```bash
# One-time setup. Copy the key that prints — it won't show again.
synckey init

# Add provider keys (prompted, hidden)
synckey key add groq
synckey key add gemini
synckey key add cerebras

# Bulk-add multiple keys for a provider
synckey key add groq --key "sk-key1,sk-key2,sk-key3"
# or from a file
synckey key import groq --file ~/groq-keys.txt

# Discover which models your keys can actually call
synckey models --refresh

# Start the gateway
synckey serve

# Watch it live
synckey dash
```

Point any OpenAI-compatible client at it:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    api_key="sk-synckey-...",  # your unified key from `synckey init`
)

# auto-routed to Gemini
client.chat.completions.create(
    model="gemini-2.0-flash",
    messages=[{"role": "user", "content": "hi"}],
)

# explicit provider prefix
client.chat.completions.create(
    model="groq/llama-3.3-70b-versatile",
    messages=[{"role": "user", "content": "hi"}],
)
```

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-synckey-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.0-flash","messages":[{"role":"user","content":"hi"}]}'
```

## CLI reference

| Command | What it does |
|---|---|
| `synckey init` | One-time setup; prints your unified key. |
| `synckey providers` | List every supported provider + where to get a key. |
| `synckey key add <provider>` | Store a credential. `--key` accepts comma-separated values for bulk. |
| `synckey key import <provider>` | Bulk import from `--file keys.txt` or `--keys k1,k2,k3`. |
| `synckey key list [provider]` | Health matrix: status, RPM cap/used, requests, errors, cost. |
| `synckey key rm <id>` | Delete a key. |
| `synckey key enable/disable <id>` | Flip a key on/off without deleting it. |
| `synckey key limits <id>` | Set `--rpm` / `--tpm` caps that drive the proactive bucket. |
| `synckey models [--refresh] [-p provider] [--tier frontier\|high\|mid\|low]` | Models your keys can actually call. |
| `synckey detect <model>` | Show routing, tier, floor, and price for any model name. |
| `synckey serve [--host --port]` | Run the gateway. |
| `synckey dash [-i seconds]` | Live dashboard: key heat, burn rates, events, recent requests. |
| `synckey usage [--hours N] [--recent]` | Token & request monitoring. |
| `synckey spend [--hours N]` | Cost breakdown by provider and model. |
| `synckey events [--type] [-n limit]` | Routing events: rate limits, key deaths, tier fallbacks. |
| `synckey status` | At-a-glance overview. |
| `synckey test [provider]` | Health-check stored keys against provider APIs. |

## How routing works

1. **Explicit prefix** — `provider/model` when the prefix is a known provider id (`groq/…`, `cohere/…`). Org-namespaced names like `meta/llama-3.3-70b` are left intact.
2. **Live model index** — providers whose `/models` endpoint advertises the model, ordered by your configured `priority`. Authoritative.
3. **Name patterns** — `gemini-*` → Gemini, `command-*` → Cohere, `claude-*` → Anthropic, etc.

Within each provider, every live key is tried before moving to the next provider.

## Model quality tiers and smart fallback

synckey detects each model's quality tier automatically:

| Tier | Examples |
|------|---------|
| FRONTIER | claude-opus-4-8, gpt-5, gemini-2.5-ultra |
| HIGH | claude-sonnet-4-6, gpt-4o, gemini-1.5-pro |
| MID | gpt-4o-mini, llama-3.3-70b, gemini-2.0-flash |
| LOW | gemma-2b, llama-3.2-1b |

**The floor rule:** fallback chains never drop below the requested model's tier. If you call Claude Opus (FRONTIER), synckey will try other FRONTIER models (GPT-5, Gemini Ultra) but never fall to a MID OSS model.

**Override the floor per-request:**

```bash
curl ... -H "X-Quality-Floor: high"  # allow HIGH-tier fallbacks for this request
```

**Smart cooling skip:** if all keys for the primary model are cooling for > 60 s, synckey immediately routes to a same-tier alternative on another provider rather than waiting behind the cooldown. No manual intervention needed.

**Override tiers in config:**

```toml
[tiers]
fallback = true
overrides = {"my-fine-tuned-model" = "frontier", "cheap-local" = "low"}
prices = {"my-fine-tuned-model" = [1.0, 3.0]}  # [input $/M, output $/M]
```

## Durable key health

Key states are persisted to SQLite immediately on every transition:

- **LIVE** — key is healthy, bucket has capacity.
- **COOLING** — key got 429'd; cooldown tracks the `Retry-After` header. Auto-recovers when the window expires, even across restarts.
- **DEAD** — key got a 401/403; excluded permanently until you manually re-enable it.

A process restart never forgets which keys are locked. With 20 keys and 18 cooling, you'll still find the 2 live ones instantly on restart.

```bash
synckey key list          # see health, cooldown remaining, dead reason
synckey key enable <id>   # manually revive a dead key after you fix it
```

## Bulk key import

```bash
# Inline comma-separated
synckey key add groq --key "sk-key1,sk-key2,sk-key3"

# From a file (one key per line; # comments ignored)
synckey key import groq --file ~/groq-keys.txt

# Comma-separated via import command
synckey key import groq --keys "sk-key1,sk-key2"

# From environment variable
synckey key import groq --from-env   # reads GROQ_API_KEY etc.

# With limits
synckey key import groq --file keys.txt --rpm 30 --tpm 6000
```

## Live dashboard

```bash
synckey dash            # refresh every 2 seconds
synckey dash -i 5       # refresh every 5 seconds
```

Shows:
- Total requests, tokens, cost; live/cooling/dead key counts
- Per-key health, RPM burn rate, request counts, cost
- Recent routing events (rate limits, key deaths, tier fallbacks)
- Last 6 requests with status and latency

Press `Ctrl+C` to exit.

## Gateway endpoints

All OpenAI-compatible:

| Endpoint | Notes |
|---|---|
| `POST /v1/chat/completions` | Streaming and non-streaming. |
| `POST /v1/embeddings` | Non-streaming. |
| `POST /v1/completions` | Legacy completions. |
| `GET  /v1/models` | Aggregated across providers; includes `synckey_tier` field. |
| `GET  /v1/usage` | Live token and cost totals (synckey extension). |
| `GET  /v1/events?limit=50` | Recent routing events (synckey extension). |
| `GET  /healthz` | Key counts (live/cooling/dead), provider list, model count. |

## Configuration

`~/.synckey/config.toml` (all optional):

```toml
[gateway]
host = "127.0.0.1"   # change to 0.0.0.0 to expose (then put nginx in front)
port = 8787
request_timeout = 120
max_connections = 600
max_keepalive = 300

[routing]
priority = ["cerebras", "groq", "nvidia"]   # tie-break provider order
cross_provider_fallback = true
max_retries = 6
default_cooldown = 20         # seconds when no Retry-After header
deep_cooling_threshold = 60   # seconds; skip to tier alts when all primary keys cooling > this

[tiers]
fallback = true
overrides = {"my-model" = "frontier"}
prices = {"my-model" = [1.0, 3.0]}

# Add any OpenAI-compatible provider not built in:
[[providers]]
id = "fireworks"
name = "Fireworks AI"
base_url = "https://api.fireworks.ai/inference/v1"
env = ["FIREWORKS_API_KEY"]
patterns = ["^accounts/fireworks/"]
```

State lives under `~/.synckey/` (override with `$SYNCKEY_HOME`):
- `secret.key` — Fernet key for sealing provider credentials (0600 perms)
- `synckey.db` — SQLite: keys, usage, events, key states, metadata
- `config.toml` — user settings and custom providers

## Custom domain / reverse proxy

The gateway binds to `127.0.0.1` by default. To expose it on a domain:

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

Then start the gateway bound to all interfaces:

```bash
synckey serve --host 0.0.0.0 --port 8787
# or in config.toml: host = "0.0.0.0"
```

## Analytics

```bash
synckey usage                     # lifetime totals
synckey usage --hours 24          # last 24 hours
synckey usage --recent            # last 25 requests with status/latency
synckey spend                     # cost breakdown by model
synckey spend --hours 1           # last hour
synckey events                    # all routing events
synckey events --type key_dead    # filter: rate_limited | key_dead | tier_fallback
```

Via HTTP (requires unified key header):

```bash
curl http://127.0.0.1:8787/v1/usage -H "Authorization: Bearer sk-synckey-..."
curl http://127.0.0.1:8787/v1/events -H "Authorization: Bearer sk-synckey-..."
curl http://127.0.0.1:8787/healthz
```

## Performance

Hot path design:
- Key state (cooldowns, buckets) lives entirely in memory — no DB read per request.
- Provider secrets decrypted once and cached.
- Usage rows handed to a background writer thread; no per-request `fsync`.
- Responses streamed straight through; non-streaming bodies passed as raw bytes.
- Async end to end (uvicorn + httpx connection pool).

Measured on a single pinned core, 200-way concurrency, 20k requests against a local upstream: **~270 req/s, 0 errors, ~83 MB RSS**.

## Security

- Provider secrets are Fernet-encrypted at rest; only the last 4 chars ever shown.
- The unified key is stored as a SHA-256 hash — keep the plaintext from `synckey init` safe.
- The gateway binds to `127.0.0.1` by default. Only expose deliberately.
- `secret.key` is written with 0600 permissions; `~/.synckey/` with 0700.

## Development

```bash
uv pip install -e ".[dev]"
pytest          # 58 tests
```

## License

MIT
