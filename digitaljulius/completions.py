"""Completion-style LLM providers.

Unlike agentic CLIs (claude/gemini/gh) which have their own tool loop,
completion providers just take text in and return text. Use them for cheap
roles: classifier, synthesiser, output-review. Anything OpenAI-compatible
(OpenRouter, Groq, DeepSeek, Together, Fireworks, Cerebras, Mistral via its
OpenAI-compat endpoint, vLLM, LM Studio) plugs in via OpenAICompat.

Each provider exposes the same .run() shape as AgentAdapter so the
orchestrator can use either kind interchangeably for the classifier/approver
roles.
"""
from __future__ import annotations

import importlib
import time
from dataclasses import dataclass
from pathlib import Path

from digitaljulius.agents.base import AgentResponse
from digitaljulius.secrets import get as get_secret


SYSTEM_DEFAULT = (
    "You are a helpful AI assistant operating inside DigitalJulius, "
    "a multi-agent orchestrator. Answer concisely unless asked otherwise."
)


@dataclass
class CompletionProvider:
    name: str
    adapter: str                # "anthropic" | "openai-compat" | "ollama"
    default_model: str
    fallback_chain: list[str]
    secret_ref: str | None = None       # key in secrets.json (None for local Ollama)
    base_url: str | None = None         # OpenAI-compat endpoint
    kind: str = "completion"

    # ---- protocol matching AgentAdapter ---------------------------------
    @property
    def command(self) -> str:
        return f"{self.adapter}://{self.base_url or self.name}"

    def is_installed(self) -> bool:
        try:
            importlib.import_module(self._sdk_module())
            return True
        except Exception:
            return False

    def is_authenticated(self) -> bool:
        if self.adapter == "ollama":
            return True   # local; auth = service running
        if self.secret_ref is None:
            return False
        return bool(get_secret(self.secret_ref))

    def credentials_path(self) -> Path:
        # CompletionProvider doesn't store creds on disk; this satisfies the
        # AgentAdapter-shaped interface used by auth.probe().
        return Path(self.secret_ref or "")

    def _sdk_module(self) -> str:
        return {
            "anthropic":     "anthropic",
            "openai-compat": "openai",
            "ollama":        "ollama",
        }.get(self.adapter, self.adapter)

    # ---- main entry point ------------------------------------------------
    def run(self, prompt: str, model: str, yolo: bool = True,
            cwd: Path | None = None, timeout: int = 120) -> AgentResponse:
        del yolo, cwd  # unused for completion-only providers
        t0 = time.time()
        try:
            if self.adapter == "anthropic":
                text = _call_anthropic(self, model, prompt, timeout)
            elif self.adapter == "openai-compat":
                text = _call_openai_compat(self, model, prompt, timeout)
            elif self.adapter == "ollama":
                text = _call_ollama(self, model, prompt, timeout)
            else:
                return AgentResponse(
                    agent=self.name, model=model, ok=False, text="",
                    stderr=f"unknown adapter: {self.adapter}",
                    duration_s=time.time() - t0,
                )
            return AgentResponse(
                agent=self.name, model=model, ok=True, text=text.strip(),
                duration_s=time.time() - t0,
            )
        except Exception as e:
            return AgentResponse(
                agent=self.name, model=model, ok=False, text="",
                stderr=f"{type(e).__name__}: {e}",
                duration_s=time.time() - t0,
            )


# ---------------------------------------------------------------------------
# adapter implementations
# ---------------------------------------------------------------------------

def _call_anthropic(p: CompletionProvider, model: str, prompt: str, timeout: int) -> str:
    import anthropic   # type: ignore
    api_key = get_secret(p.secret_ref) if p.secret_ref else None
    if not api_key:
        raise RuntimeError("anthropic provider has no API key in vault")
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    msg = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_DEFAULT,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
    return "".join(parts)


def _call_openai_compat(p: CompletionProvider, model: str, prompt: str, timeout: int) -> str:
    import openai   # type: ignore
    api_key = get_secret(p.secret_ref) if p.secret_ref else "EMPTY"
    client = openai.OpenAI(
        api_key=api_key or "EMPTY",
        base_url=p.base_url or "https://api.openai.com/v1",
        timeout=timeout,
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_DEFAULT},
            {"role": "user", "content": prompt},
        ],
        max_tokens=4096,
    )
    return resp.choices[0].message.content or ""


def _call_ollama(p: CompletionProvider, model: str, prompt: str, timeout: int) -> str:
    try:
        import ollama   # type: ignore
        client = ollama.Client(host=p.base_url or "http://localhost:11434")
        resp = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_DEFAULT},
                {"role": "user", "content": prompt},
            ],
        )
        return resp.get("message", {}).get("content", "") or ""
    except ImportError:
        # Fall back to raw HTTP so users without the ollama sdk still work.
        import urllib.request
        import json as _json
        body = _json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_DEFAULT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }).encode("utf-8")
        url = (p.base_url or "http://localhost:11434") + "/api/chat"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = _json.loads(r.read().decode("utf-8"))
        return data.get("message", {}).get("content", "") or ""


# ---------------------------------------------------------------------------
# Built-in adapter recipes — used by /llm add for one-line provisioning.
# ---------------------------------------------------------------------------

# Each entry: friendly name → (adapter, default base_url, default_model,
# fallback_chain, secret_ref or None). User can override after add via TOML.
BUILTIN_RECIPES: dict[str, dict] = {
    "anthropic-api": {
        "adapter": "anthropic",
        "default_model": "claude-opus-4-7",
        "fallback_chain": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        "secret_ref": "anthropic-api",
        "needs_key": True,
        "key_hint": "ANTHROPIC API key — get one at console.anthropic.com",
    },
    "openai": {
        "adapter": "openai-compat",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "fallback_chain": ["gpt-4o", "gpt-4o-mini"],
        "secret_ref": "openai",
        "needs_key": True,
        "key_hint": "OPENAI API key — get one at platform.openai.com",
    },
    "openrouter": {
        "adapter": "openai-compat",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-opus-4-7",
        "fallback_chain": [
            "anthropic/claude-opus-4-7",
            "google/gemini-2.5-pro",
            "meta-llama/llama-3.3-70b-instruct",
        ],
        "secret_ref": "openrouter",
        "needs_key": True,
        "key_hint": "OpenRouter key — get one at openrouter.ai/keys (free tier exists)",
    },
    "groq": {
        "adapter": "openai-compat",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "fallback_chain": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
        "secret_ref": "groq",
        "needs_key": True,
        "key_hint": "Groq key — get one at console.groq.com (free tier)",
    },
    "deepseek": {
        "adapter": "openai-compat",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "fallback_chain": ["deepseek-chat", "deepseek-reasoner"],
        "secret_ref": "deepseek",
        "needs_key": True,
        "key_hint": "DeepSeek key — get one at platform.deepseek.com",
    },
    "together": {
        "adapter": "openai-compat",
        "base_url": "https://api.together.xyz/v1",
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "fallback_chain": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"],
        "secret_ref": "together",
        "needs_key": True,
        "key_hint": "Together AI key",
    },
    "mistralai": {
        "adapter": "openai-compat",
        "base_url": "https://api.mistral.ai/v1",
        "default_model": "mistral-large-latest",
        "fallback_chain": ["mistral-large-latest", "mistral-small-latest"],
        "secret_ref": "mistralai",
        "needs_key": True,
        "key_hint": "Mistral AI key",
    },
    "ollama": {
        "adapter": "ollama",
        "base_url": "http://localhost:11434",
        "default_model": "llama3.3",
        "fallback_chain": ["llama3.3", "qwen2.5-coder"],
        "secret_ref": None,
        "needs_key": False,
        "key_hint": "Ollama runs locally — no key needed. `ollama serve` must be running.",
    },
}
