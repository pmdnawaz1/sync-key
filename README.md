# synckey

**Merge every AI provider key behind one unified, OpenAI-compatible API key.**

Gemini, Groq, NVIDIA, GitHub Models, Cerebras, Cohere, Mistral, DeepSeek, xAI,
OpenAI, OpenRouter, Together, SambaNova, Ollama and any custom OpenAI-compatible
provider all behind a single key you point your OpenAI SDK at.

No frontend. No Redis. Just a CLI and a fast local gateway.

```
                           your app
                    ┌────── (OpenAI SDK) ───────┐
                    │                           │
                    ▼                           │
    base_url: http://127.0.0.1:8787/v1          │
    api_key: sk-synckey-...                     │
                    │                           │
                    ▼                           │
┌──────────────────────────────────────────┐   │
│           synckey gateway                  │◀──┘
│                                          │
│  1. auth        ────► your app           │
│  2. resolve     ────► model + providers   │
│  3. route       ────► key pool            │
│  4. failover    ────► tier fallback      │
│  5. forward     ────► upstream provider   │
│  6. stream/log  ────► response            │
└──────────────────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
        Groq          Gemini         Cerebras
        (key #1)      (key #1)       (key #1)
```

---

## Quick start

```bash
# Option A: Install in a virtual environment (recommended)
py -m venv .venv && .venv\Scripts\activate
pip install -e .

# Option B: Install system-wide
pip install .
```

**After system-wide install**, add the Python Scripts directory to your PATH:

**cmd.exe:**
```cmd
setx PATH "%PATH%;C:\Users\pmdna\AppData\Local\Python\pythoncore-3.14-64\Scripts"
```
*(Restart cmd for the change to take effect, or run `set PATH=%PATH%;C:\Users\pmdna\AppData\Local\Python\pythoncore-3.14-64\Scripts` for just that session)*

**PowerShell / Bash (Git Bash, WSL, etc.):**
```bash
export PATH="$PATH:/c/Users/pmdna/AppData/Local/Python/pythoncore-3.14-64/Scripts"
```
*(Add to ~/.bashrc or ~/.zshrc to persist)*

Then run the guided setup:

```bash
synckey setup     # guided: mint key → add provider keys → fetch models → set defaults
synckey serve     # start the gateway
```

`synckey setup` walks you through it interactively. It:
1. Mints your unified key (shown once **store it safely**)
2. Collects provider API keys (single, comma-separated, or `--file`)
3. Fetches the models those keys can call
4. Asks for a **default model** so clients can omit it

---

## How to connect your app

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    api_key="sk-synckey-...",   # your unified key from `synckey setup`
)

# Use your default model client stays simple.
client.chat.completions.create(messages=[{"role": "user", "content": "hi"}])

# Name a model explicitly synckey routes it automatically.
client.chat.completions.create(model="gemini-2.0-flash", messages=[...])

# Prefer a specific provider (hint, not forced).
client.chat.completions.create(
    model="llama-3.3-70b",
    extra_body={"provider": "groq"}      # Groq gets first dibs; others used on fallback
)
```

**Via curl:**

```bash
# Use your default model (set during setup)
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-synckey-..." \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}]}'

# Or name a model explicitly — routes automatically
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-synckey-..." \
  -H "Content-Type: application/json" \
  -d '{"model": "openai/gpt-oss-120b", "messages":[{"role":"user","content":"hi"}]}'

# Force a specific provider with X-Provider header
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-synckey-..." \
  -H "X-Provider: groq" \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

### The routing hint (`provider` / `X-Provider`)

| What you send | What synckey does |
|---|---|
| `model: "gemini-2.0-flash"` | Routes by model name live index → name pattern → default |
| `model: "groq/llama-3.3-70b"` | **Forced** to Groq (explicit prefix) |
| `extra_body={"provider": "groq"}` | Hint: Groq first, others on fallback |
| `model` omitted | Uses your **global default model** |
| `model` omitted + `provider: "groq"` | Uses **Groq's default model** |

`synckey detect <model>` shows exactly how any model will route.

---

## How routing decides

```
client request
     │
     ▼
resolve(model) → lookup live index → ordered by priority
     │
     ├─ explicit "provider/model"  → forced to that provider
     ├─ known model in index       → providers in priority order
     └─ unknown model              → name pattern match (gemini-*, command-*, etc.)
     │
     ▼
key pool: pick ready key per provider, in order
     │
     ├─ key READY   → forward immediately
     ├─ key THROTTLED → try anyway (may 429, caught by bucket)
     └─ key COOLING  → skip unless no other option
     │
     ▼ (if all primary keys cooling > 60s)
tier fallback: find same-tier model on another provider
     │
     ▼
upstream provider response
```

### Routing priorities

synckey tries every live key within a provider before moving to the next:

```bash
# Which provider wins when multiple have the model?
synckey routing priority groq gemini   # Groq first, then Gemini

# See the current order
synckey routing priority
```

---

## How failover works

synckey classifies every model into a **quality tier**:

| Tier | Examples |
|------|----------|
| FRONTIER | claude-opus-4-8, gpt-5, gemini-2.5-pro |
| HIGH | claude-sonnet-4-6, gpt-4o, gemini-1.5-pro |
| MID | gpt-4o-mini, llama-3.3-70b, gemini-2.0-flash |
| LOW | gemma-2b, llama-3.2-1b |

**The floor rule:** fallback chains **never drop below** the requested model's tier.

```
You call: claude-opus-4-8 (FRONTIER)

synckey tries:
  1. claude-opus-4-8 on Anthropic      ✗ (rate limited)
  2. gpt-5 on OpenAI                  ✗ (rate limited)
  3. gemini-2.5-pro on Gemini         ✓ success!

It will NOT fall to MID-tier models like gpt-4o-mini.
```

**Override the floor per request:**
```bash
-H "X-Quality-Floor: high"    # refuse to use MID or LOW tier
```

**Routing presets** choose your fallback philosophy:

```bash
# reliable: never drop tier, try hard to find same-tier alternative
synckey routing preset reliable

# cheap: prefer cheaper models, drop tiers more freely
synckey routing preset cheap

# Show current preset
synckey routing preset
```

**Smart cooling skip:** if all primary keys are cooling for **> 60 seconds**, synckey immediately jumps to a same-tier alternative rather than waiting behind the cooldown.

**Override a tier permanently:**
```bash
synckey tier set my-fine-tuned-model frontier
synckey tier unset my-fine-tuned-model   # revert to auto-detected
synckey tier list                         # see all overrides
```

---

## Test your setup

```bash
# Test a provider + model combination with N calls
synckey test groq llama-3.3-70b-versatile 5

# Shows per-call: which key used, latency, tokens, status
# If all keys fail, shows the error and which fallback was attempted

# Test which route a model would take (dry-run, no actual call)
synckey detect gemini-2.0-flash

# Health-check all stored keys
synckey test
```

---

## Live dashboard

```bash
synckey dash              # refresh every 2 seconds
synckey dash -i 5         # every 5 seconds
```

Four views, switch with number keys:

| Key | View | What it shows |
|-----|------|---------------|
| **1** | Overview | Keys health + recent requests + events summary |
| **2** | Keys | Per-key RPM, requests, cost, health status |
| **3** | Events | Full routing events log (fallbacks, rate limits, dead keys) |
| **4** | Routing | Last request's full resolution chain |

```
Keys: [1] overview  [2] keys  [3] events  [4] routing  [p] pause  [r] refresh  [q] quit
```

---

## When keys are rate-limited (deferred requests)

If **every** key is cooling or throttled, synckey queues the request instead of failing:

```bash
# Your POST gets a 202 with a poll URL
{
  "id": "defer_AbC123",
  "status": "queued",
  "result_url": "/v1/requests/defer_AbC123",
  "retry_after": 28
}

# Poll later
curl :8787/v1/requests/defer_AbC123 -H "Authorization: Bearer sk-synckey-..."
# → 200 + the actual completion once a key ran it
```

```bash
synckey deferred list     # queued / running / done, with ETAs
synckey deferred get <id> # full status + response
```

This is **automatic** no code changes needed. Turn it off in `config.toml`:
```toml
[deferred]
enabled = false   # get 503 instead of 202 when all keys are cooling
```

---

## Key management

```bash
# Add a provider key
synckey key add groq                        # prompted, hidden input
synckey key add groq --key "sk-1,sk-2"      # comma-separated
synckey key add groq --file ~/keys.txt      # one per line
synckey key add groq --from-env             # reads GROQ_API_KEY

# After adding, synckey fetches the models your key can call
# and offers to set that provider's default model

# Manage keys
synckey key list              # health, RPM, requests, cost, cooldown remaining
synckey key rm <id>           # delete
synckey key disable <id>      # turn off without deleting (revive with enable)
synckey key limits <id> --rpm 100 --tpm 100000   # set rate caps
synckey key health            # health-check all stored keys
```

Key states persist across restarts:
- **LIVE** healthy, bucket has capacity
- **COOLING** got 429'd, tracks `Retry-After` header
- **DEAD** got 401/403, excluded until re-enabled

---

## Defaults and aliases

A default can be:
- A **model id** — e.g. `gemini-2.0-flash` or `llama-3.3-70b-versatile`
- A **tier** — `frontier`, `high`, `mid`, or `low` — synckey picks the best available model at that tier
- An **alias** — a name you define that maps to a model or tier

```bash
synckey config set-default gemini-2.0-flash       # specific model
synckey config set-default mid                    # best MID-tier model available
synckey config set-default fast                    # an alias you've defined

synckey config provider-default groq llama-3.3-70b-versatile  # per-provider
synckey config alias fast mid                      # "fast" → best MID-tier model
synckey config alias smart frontier                # "smart" → best FRONTIER model
synckey config unalias fast

synckey config             # see everything in one place
```

**Tiers** (auto-detected for every model):

| Tier | What it means | Example models |
|------|--------------|----------------|
| `frontier` | Best quality | claude-opus-4-8, gpt-5, gemini-2.5-pro |
| `high` | High quality | claude-sonnet-4-6, gpt-4o |
| `mid` | Balanced speed/cost | gpt-4o-mini, llama-3.3-70b, gemini-2.0-flash |
| `low` | Fast/cheap | gemma-2b, llama-3.2-1b |

When you set a tier as default, synckey resolves it to the best live model at that tier when a request comes in.

---

## Gateway endpoints

All OpenAI-compatible:

| Endpoint | Notes |
|---|---|
| `POST /v1/chat/completions` | Streaming and non-streaming |
| `POST /v1/embeddings` | Non-streaming |
| `POST /v1/completions` | Legacy completions |
| `GET /v1/requests/{id}` | Poll a deferred request |
| `GET /v1/models` | Aggregated across providers |
| `GET /v1/usage` | Token and cost totals |
| `GET /v1/events?limit=50` | Recent routing events |
| `GET /healthz` | Key counts, providers, model count |

---

## Configuration reference

Two stores:

- **CLI (DB):** defaults, aliases, tier overrides → `synckey config`, `synckey tier`
- **File (`~/.synckey/config.toml`):** static infrastructure

```toml
[gateway]
host = "127.0.0.1"   # use 0.0.0.0 and put nginx/caddy in front to expose
port = 8787
request_timeout = 120

[routing]
priority = ["groq", "gemini"]      # provider preference order
tier_fallback = true                # enable cross-tier fallback
max_retries = 6
default_cooldown = 20               # seconds when upstream sends no Retry-After
deep_cooling_threshold = 60        # skip to tier alts after this many seconds cooling

[tiers]
overrides = {"my-model" = "frontier"}
prices = {"my-model" = [1.0, 3.0]}   # [input $/M, output $/M]

[deferred]
enabled = true
ttl = 3600           # keep finished responses this long (seconds)
poll = 5             # worker scan interval
max_queue = 1000

# Add a custom OpenAI-compatible provider:
[[providers]]
id = "fireworks"
name = "Fireworks AI"
base_url = "https://api.fireworks.ai/inference/v1"
env = ["FIREWORKS_API_KEY"]
patterns = ["^accounts/fireworks/"]
```

State lives under `~/.synckey/` (override with `$SYNCKEY_HOME`):
- `secret.key` Fernet key (0600)
- `synckey.db` SQLite
- `config.toml` static settings

---

## Expose behind a domain

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
        flush_interval -1
    }
}
```

```bash
synckey serve --host 0.0.0.0 --port 8787
```

---

## Monitoring

```bash
synckey dash              # live TUI dashboard
synckey status            # at-a-glance overview
synckey usage             # lifetime totals
synckey usage --hours 24  # last 24 hours
synckey usage --recent    # last 25 requests
synckey spend             # cost breakdown by model
synckey events            # routing events log
synckey events --type key_dead    # filter by type
```

---

## Full CLI reference

```
Setup
  synckey setup                 guided first-run
  synckey init                  mint the unified key only
  synckey --guide               interactive walkthrough (any time)

Gateway
  synckey serve [--host --port] run the gateway
  synckey dash [-i N]           live dashboard (1/2/3/4 views, p/r/q)

Connect
  synckey test [prov] [model] [N]   test route with N actual calls
  synckey detect <model>            show routing path (dry-run)
  synckey routing priority [list]   show/set provider preference order
  synckey routing preset [name]     reliable | cheap | show current

Keys
  synckey key add <provider> [--key|--file|--from-env]
  synckey key list|rm|enable|disable|limits
  synckey key health

Config
  synckey config [set-default|provider-default|alias|unalias]
  synckey tier set|unset|list <model> [tier]

Models
  synckey providers             list supported providers
  synckey models [--refresh] [-p prov] [--tier ...]

Monitoring
  synckey usage [--hours N] [--recent]
  synckey spend [--hours N]
  synckey events [--type] [-n limit]
  synckey deferred list|get
  synckey status
```

---

## Security

- Provider secrets are Fernet-encrypted at rest
- Unified key stored as SHA-256 hash
- Gateway binds to `127.0.0.1` by default
- `secret.key` is 0600; `~/.synckey/` is 0700

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
