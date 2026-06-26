# synckey

**Merge every AI provider key behind one unified, OpenAI-compatible API key.**

Gemini, Groq, NVIDIA, GitHub Models, Cerebras, Cohere, Mistral, DeepSeek, xAI,
OpenAI, OpenRouter, Together, SambaNova, Ollama — and any custom provider — all
fronted by a single key you point your OpenAI client at. synckey routes each
request to the right provider, rotates across your keys round-robin, **eats rate
limits** by failing over the instant a key gets 429'd, and meters every token.

No frontend. Just a CLI and a fast local gateway.

```
                         ┌──────────────────────────────────────┐
  your app               │            synckey gateway           │
  (OpenAI SDK) ──────────▶  auth → route → key pool → failover  │──▶ Groq
  base_url=:8787/v1      │         ▲             │  (eat 429s)   │──▶ Gemini
  api_key=sk-synckey-... │         │             ▼               │──▶ Cerebras
                         │   model index    token metering (SQLite)│──▶ Cohere …
                         └──────────────────────────────────────┘
```

## Why

Every provider hands you a different key, a different base URL, different rate
limits, and a different model catalog. synckey collapses all of that into one
key and one endpoint:

- **One unified key** — `sk-synckey-…`. Your apps never see the real provider keys.
- **Provider auto-detection** — `gemini-2.0-flash` goes to Gemini, `command-r`
  to Cohere, `groq/llama-3.3-70b-versatile` to Groq. Explicit prefix → live
  model index → name patterns.
- **Round-robin + weights** — add several keys per provider; load spreads across them.
- **Rate-limit eater** — on `429`/`5xx`, the key is cooled down (honoring
  `Retry-After`) and the request *immediately* fails over to the next live
  key, then the next provider that serves the model. Traffic keeps flowing.
- **Full token monitoring** — every request (streaming included) logged to
  SQLite: prompt/completion tokens, latency, status, per-provider, per-model.
- **Model discovery** — `synckey models` queries your keys and shows exactly
  which models you can actually call.
- **Encrypted at rest** — provider secrets are Fernet-sealed; the DB never holds plaintext.

## Install

```bash
uv venv && uv pip install -e .
# or: pip install -e .
```

## Quickstart

```bash
synckey init                      # mint your unified key (shown once)
synckey key add groq              # paste your Groq key (prompted, hidden)
synckey key add gemini            # …add as many providers as you like
synckey key add groq --label groq-2   # multiple keys per provider → round-robin
synckey models --refresh          # discover what those keys can call
synckey serve                     # start the gateway on :8787
```

Then point any OpenAI-compatible client at it:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-synckey-...")

# auto-detected → Gemini
client.chat.completions.create(model="gemini-2.0-flash",
                               messages=[{"role": "user", "content": "hi"}])

# explicit provider prefix → Groq
client.chat.completions.create(model="groq/llama-3.3-70b-versatile",
                               messages=[{"role": "user", "content": "hi"}])
```

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-synckey-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.0-flash","messages":[{"role":"user","content":"hi"}]}'
```

## CLI

| Command | What it does |
|---|---|
| `synckey init` | One-time setup; prints your unified key. |
| `synckey providers` | List every supported provider + where to get a key. |
| `synckey key add <provider>` | Store a credential (`--key`, `--from-env`, `--label`, `--weight`). Repeatable. |
| `synckey key list` | Show stored keys, health, and cooldown state. |
| `synckey key rm/enable/disable <id>` | Manage individual keys. |
| `synckey models [--refresh] [-p <provider>]` | Models your keys can actually call. |
| `synckey detect <model>` | Show how a model name routes (prefix/index/pattern). |
| `synckey serve [--host --port]` | Run the gateway. |
| `synckey usage [--hours N] [--recent]` | Token & request monitoring. |
| `synckey status` | At-a-glance overview. |
| `synckey test [provider]` | Health-check stored keys. |

## Gateway endpoints (OpenAI-compatible)

- `POST /v1/chat/completions` — streaming and non-streaming
- `POST /v1/embeddings`, `POST /v1/completions`
- `GET  /v1/models` — aggregated across your configured providers
- `GET  /v1/usage` — live token totals (synckey extension)
- `GET  /healthz`

## How routing works

1. **Explicit prefix** — `provider/model` when the prefix is a known provider
   id (`groq/…`, `cohere/…`). Org-namespaced names like `meta/llama-3.3-70b`
   are left intact (`meta` isn't a provider).
2. **Live model index** — providers whose `/models` actually advertise the
   model, ordered by your configured `priority`. Authoritative.
3. **Name patterns** — `gemini-*` → Gemini, `command-*` → Cohere, etc.

When several providers serve the same model, synckey tries them in priority
order, and within each provider it tries every live key before moving on.

## Configuration

`~/.synckey/config.toml` (all optional):

```toml
[gateway]
host = "127.0.0.1"
port = 8787
request_timeout = 120

[routing]
priority = ["cerebras", "groq", "nvidia"]   # tie-break when many serve a model
cross_provider_fallback = true
max_retries = 4                              # per-request attempts across the pool
default_cooldown = 20                        # seconds, when no Retry-After given

# Add any OpenAI-compatible provider not built in:
[[providers]]
id = "fireworks"
name = "Fireworks AI"
base_url = "https://api.fireworks.ai/inference/v1"
env = ["FIREWORKS_API_KEY"]
patterns = ["^accounts/fireworks/"]
```

State lives under `~/.synckey/` (override with `$SYNCKEY_HOME`):
`secret.key` (0600 Fernet key), `synckey.db` (SQLite), `config.toml`.

## Security notes

- Provider secrets are encrypted at rest; only their last 4 chars are ever shown.
- The unified key is stored as a SHA-256 hash — keep the plaintext from `init` safe.
- The gateway binds to `127.0.0.1` by default. Only expose it deliberately.

## Development

```bash
uv pip install -e ".[dev]"
pytest
```

## License

MIT
