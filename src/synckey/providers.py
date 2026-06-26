"""Built-in provider registry.

Every provider here exposes an OpenAI-compatible REST surface, so a single
adapter (one base URL + a Bearer key) can talk to all of them.  Each provider
declares:

* ``base_url``       – OpenAI-compatible root (``/chat/completions`` is appended)
* ``models_path``    – relative path for the model-listing endpoint
* ``env``            – env vars commonly used for that provider's key
* ``patterns``       – regexes used for static model -> provider auto-detection
* ``auth``           – how the credential is attached to outbound requests
* ``signup``         – where a user gets a key (shown in the CLI)

The registry is intentionally data-only so it can be merged with user-defined
providers from config without special-casing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Pattern


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    base_url: str
    models_path: str = "/models"
    env: tuple[str, ...] = ()
    # Raw regex strings; compiled lazily into ``_compiled``.
    patterns: tuple[str, ...] = ()
    auth: str = "bearer"  # "bearer" | "x-api-key" | "query:<param>"
    signup: str = ""
    notes: str = ""

    @property
    def compiled(self) -> list[Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.patterns]

    def matches(self, model: str) -> bool:
        return any(rx.search(model) for rx in self.compiled)

    def auth_headers(self, secret: str) -> dict[str, str]:
        if self.auth == "bearer":
            return {"Authorization": f"Bearer {secret}"}
        if self.auth == "x-api-key":
            return {"x-api-key": secret}
        return {}

    def auth_params(self, secret: str) -> dict[str, str]:
        if self.auth.startswith("query:"):
            return {self.auth.split(":", 1)[1]: secret}
        return {}


# --- Built-in providers ------------------------------------------------------
# Patterns are deliberately conservative: they only claim models that are
# *unambiguously* owned by a provider.  Shared open-weight names (llama, qwen,
# deepseek...) are resolved at runtime via the live model index instead, so we
# don't hard-code a wrong guess.

BUILTIN: dict[str, Provider] = {
    p.id: p
    for p in (
        Provider(
            id="gemini",
            name="Google Gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            models_path="/models",
            env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            patterns=(r"^gemini[-/]", r"^gemma[-/]?", r"^learnlm", r"^text-embedding-00"),
            signup="https://aistudio.google.com/apikey",
        ),
        Provider(
            id="groq",
            name="Groq",
            base_url="https://api.groq.com/openai/v1",
            env=("GROQ_API_KEY",),
            patterns=(r"versatile$", r"instant$", r"^whisper-large", r"^groq/"),
            signup="https://console.groq.com/keys",
        ),
        Provider(
            id="cerebras",
            name="Cerebras",
            base_url="https://api.cerebras.ai/v1",
            env=("CEREBRAS_API_KEY",),
            patterns=(r"^llama3\.1-", r"^llama-3\.3-70b$", r"^cerebras/"),
            signup="https://cloud.cerebras.ai",
        ),
        Provider(
            id="nvidia",
            name="NVIDIA NIM",
            base_url="https://integrate.api.nvidia.com/v1",
            env=("NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY"),
            patterns=(r"^nvidia/", r"nemotron"),
            signup="https://build.nvidia.com",
            notes="Models are org-namespaced, e.g. meta/llama-3.3-70b-instruct.",
        ),
        Provider(
            id="github",
            name="GitHub Models",
            base_url="https://models.github.ai/inference",
            models_path="/catalog/models",
            env=("GITHUB_TOKEN", "GITHUB_MODELS_TOKEN"),
            patterns=(r"^github/",),
            signup="https://github.com/settings/personal-access-tokens",
            notes="Use a fine-grained PAT with the models:read permission.",
        ),
        Provider(
            id="cohere",
            name="Cohere",
            base_url="https://api.cohere.ai/compatibility/v1",
            env=("COHERE_API_KEY", "CO_API_KEY"),
            patterns=(r"^command", r"^embed-", r"^rerank", r"^cohere/"),
            signup="https://dashboard.cohere.com/api-keys",
        ),
        Provider(
            id="openrouter",
            name="OpenRouter",
            base_url="https://openrouter.ai/api/v1",
            env=("OPENROUTER_API_KEY",),
            patterns=(r"^openrouter/",),
            signup="https://openrouter.ai/keys",
        ),
        Provider(
            id="together",
            name="Together AI",
            base_url="https://api.together.xyz/v1",
            env=("TOGETHER_API_KEY",),
            patterns=(r"^together/",),
            signup="https://api.together.ai/settings/api-keys",
        ),
        Provider(
            id="mistral",
            name="Mistral AI",
            base_url="https://api.mistral.ai/v1",
            env=("MISTRAL_API_KEY",),
            patterns=(r"^mistral", r"^codestral", r"^ministral", r"^pixtral", r"^magistral"),
            signup="https://console.mistral.ai/api-keys",
        ),
        Provider(
            id="deepseek",
            name="DeepSeek",
            base_url="https://api.deepseek.com/v1",
            env=("DEEPSEEK_API_KEY",),
            patterns=(r"^deepseek-chat$", r"^deepseek-reasoner$"),
            signup="https://platform.deepseek.com/api_keys",
        ),
        Provider(
            id="xai",
            name="xAI Grok",
            base_url="https://api.x.ai/v1",
            env=("XAI_API_KEY",),
            patterns=(r"^grok",),
            signup="https://console.x.ai",
        ),
        Provider(
            id="sambanova",
            name="SambaNova",
            base_url="https://api.sambanova.ai/v1",
            env=("SAMBANOVA_API_KEY",),
            patterns=(r"^sambanova/",),
            signup="https://cloud.sambanova.ai/apis",
        ),
        Provider(
            id="openai",
            name="OpenAI",
            base_url="https://api.openai.com/v1",
            env=("OPENAI_API_KEY",),
            patterns=(r"^gpt-", r"^o[0-9]", r"^chatgpt", r"^dall-e", r"^text-embedding-3"),
            signup="https://platform.openai.com/api-keys",
        ),
        Provider(
            id="ollama",
            name="Ollama (local)",
            base_url="http://localhost:11434/v1",
            env=("OLLAMA_HOST",),
            patterns=(r"^ollama/",),
            auth="bearer",
            signup="https://ollama.com",
            notes="No key required for a default local install.",
        ),
    )
}


def explicit_prefix(model: str, known_ids: set[str]) -> tuple[str | None, str]:
    """Split a ``provider/model`` prefix when the prefix is a known provider.

    Returns ``(provider_id, bare_model)``.  Org-namespaced names such as
    ``meta/llama-3.3-70b-instruct`` are *not* treated as a provider prefix
    because ``meta`` is not a registered provider, so they pass through intact.
    """
    if "/" in model:
        head, tail = model.split("/", 1)
        if head.lower() in known_ids:
            return head.lower(), tail
    return None, model


def detect_by_pattern(model: str, providers: dict[str, Provider]) -> str | None:
    """Best-effort static detection from the model name alone."""
    for pid, prov in providers.items():
        if prov.matches(model):
            return pid
    return None
