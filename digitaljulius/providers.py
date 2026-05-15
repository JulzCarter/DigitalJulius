"""Unified provider registry.

Combines two kinds of providers:
  - "agentic"    — file-editing CLIs (claude, gemini, gh) from agents/.
  - "completion" — text-in/text-out HTTP APIs (anthropic, openrouter, ollama, ...).

User-added providers live in ~/.digitaljulius/providers.toml. Built-ins are
registered in code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib   # py3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib   # type: ignore

import tomli_w

from digitaljulius.agents.registry import AGENTS
from digitaljulius.completions import BUILTIN_RECIPES, CompletionProvider
from digitaljulius.config import DJ_HOME, ensure_dirs

PROVIDERS_PATH = DJ_HOME / "providers.toml"


# Cache so we don't re-read the TOML on every call. Invalidated on add/remove.
_user_cache: dict[str, CompletionProvider] | None = None


def _load_user_providers() -> dict[str, CompletionProvider]:
    global _user_cache
    if _user_cache is not None:
        return _user_cache
    ensure_dirs()
    if not PROVIDERS_PATH.exists():
        _user_cache = {}
        return _user_cache
    try:
        with PROVIDERS_PATH.open("rb") as f:
            data = tomllib.load(f)
    except Exception:
        _user_cache = {}
        return _user_cache
    out: dict[str, CompletionProvider] = {}
    for name, cfg in (data.get("providers", {}) or {}).items():
        if cfg.get("kind", "completion") != "completion":
            continue
        out[name] = CompletionProvider(
            name=name,
            adapter=cfg.get("adapter", "openai-compat"),
            default_model=cfg.get("default_model", ""),
            fallback_chain=cfg.get("fallback_chain") or [cfg.get("default_model", "")],
            secret_ref=cfg.get("secret_ref"),
            base_url=cfg.get("base_url"),
        )
    _user_cache = out
    return out


def _invalidate() -> None:
    global _user_cache
    _user_cache = None


# ---------------------------------------------------------------------------
# public API — single entry point that hides agentic vs completion distinction
# ---------------------------------------------------------------------------

def get_provider(name: str):
    """Look up a provider by name. Raises KeyError if unknown."""
    if name in AGENTS:
        return AGENTS[name]
    users = _load_user_providers()
    if name in users:
        return users[name]
    raise KeyError(f"unknown provider: {name!r}")


def list_providers() -> dict[str, Any]:
    """Return {name: provider} for all known providers, agentic + completion."""
    out: dict[str, Any] = dict(AGENTS)
    out.update(_load_user_providers())
    return out


def list_user_providers() -> dict[str, CompletionProvider]:
    return dict(_load_user_providers())


def add_user_provider(
    name: str,
    *,
    adapter: str,
    default_model: str,
    fallback_chain: list[str] | None = None,
    secret_ref: str | None = None,
    base_url: str | None = None,
) -> CompletionProvider:
    ensure_dirs()
    data: dict = {}
    if PROVIDERS_PATH.exists():
        try:
            with PROVIDERS_PATH.open("rb") as f:
                data = tomllib.load(f)
        except Exception:
            data = {}
    data.setdefault("providers", {})
    data["providers"][name] = {
        "kind": "completion",
        "adapter": adapter,
        "default_model": default_model,
        "fallback_chain": fallback_chain or [default_model],
        **({"secret_ref": secret_ref} if secret_ref else {}),
        **({"base_url": base_url} if base_url else {}),
    }
    with PROVIDERS_PATH.open("wb") as f:
        tomli_w.dump(data, f)
    _invalidate()
    return CompletionProvider(
        name=name,
        adapter=adapter,
        default_model=default_model,
        fallback_chain=fallback_chain or [default_model],
        secret_ref=secret_ref,
        base_url=base_url,
    )


def remove_user_provider(name: str) -> bool:
    if not PROVIDERS_PATH.exists():
        return False
    try:
        with PROVIDERS_PATH.open("rb") as f:
            data = tomllib.load(f)
    except Exception:
        return False
    if name not in (data.get("providers") or {}):
        return False
    del data["providers"][name]
    with PROVIDERS_PATH.open("wb") as f:
        tomli_w.dump(data, f)
    _invalidate()
    return True


def recipe_for(name: str) -> dict | None:
    return BUILTIN_RECIPES.get(name)


def agentic_names() -> list[str]:
    return list(AGENTS.keys())


def completion_names() -> list[str]:
    return list(_load_user_providers().keys())
