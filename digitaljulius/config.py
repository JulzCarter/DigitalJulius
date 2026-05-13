"""Config and runtime-state paths."""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import tomllib  # py3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

import tomli_w

# Force UTF-8 on Windows consoles so emoji and box-drawing don't crash.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

HOME = Path(os.path.expanduser("~"))
DJ_HOME = HOME / ".digitaljulius"
CONFIG_PATH = DJ_HOME / "config.toml"
STATE_PATH = DJ_HOME / "state.json"
BUDGET_PATH = DJ_HOME / "budget.json"
LOG_DIR = DJ_HOME / "logs"

DEFAULT_CONFIG = {
    "general": {
        "yolo_default": True,
        "approver_mode": "advisory",  # "advisory" or "gatekeeper"
        "auto_spawn_twin": False,     # ask before twin-instance
    },
    "agents": {
        "claude": {
            "enabled": True,
            "command": "claude",
            "top_model": "opus",
            "fallback_chain": ["opus", "sonnet", "haiku"],
        },
        "gemini": {
            "enabled": True,
            "command": "gemini",
            "top_model": "gemini-2.5-pro",
            "fallback_chain": [
                "gemini-2.5-pro",
                "gemini-2.5-flash",
                "gemini-2.5-flash-lite",
                "gemini-2.0-flash",
                "gemini-1.5-flash",
            ],
        },
        "qwen": {
            "enabled": True,
            "command": "qwen",
            "top_model": "qwen3-coder-plus",
            "fallback_chain": ["qwen3-coder-plus", "qwen3-coder"],
        },
    },
    "budget": {
        # Conservative free-tier daily request caps.
        # Tune in ~/.digitaljulius/config.toml as quotas evolve.
        "warn_pct": 0.75,
        "switch_pct": 0.90,
        "daily_caps": {
            "claude:opus": 100,
            "claude:sonnet": 500,
            "claude:haiku": 1000,
            "gemini:gemini-2.5-pro": 1000,
            "gemini:gemini-2.5-flash": 1500,
            "gemini:gemini-2.5-flash-lite": 2000,
            "gemini:gemini-2.0-flash": 1500,
            "gemini:gemini-1.5-flash": 50,
            "qwen:qwen3-coder-plus": 2000,
            "qwen:qwen3-coder": 2000,
        },
    },
    "routing": {
        # Capability matrix — agent priority by task tag.
        # First entry is preferred; subsequent are fallbacks.
        "architecture": ["claude", "gemini", "qwen"],
        "refactor": ["claude", "qwen", "gemini"],
        "quick_edit": ["qwen", "gemini", "claude"],
        "long_context": ["gemini", "claude", "qwen"],
        "web_search": ["gemini", "claude", "qwen"],
        "math": ["claude", "gemini", "qwen"],
        "default": ["claude", "gemini", "qwen"],
    },
    "approver": {
        "agent": "claude",
        "model": "opus",
    },
    "classifier": {
        "agent": "gemini",
        "model": "gemini-2.5-flash",
    },
}


def ensure_dirs() -> None:
    DJ_HOME.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    with CONFIG_PATH.open("rb") as f:
        loaded = tomllib.load(f)
    # Merge with defaults so missing keys don't break things.
    merged = _deep_merge(DEFAULT_CONFIG, loaded)
    return merged


def save_config(cfg: dict) -> None:
    ensure_dirs()
    with CONFIG_PATH.open("wb") as f:
        tomli_w.dump(cfg, f)


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result
