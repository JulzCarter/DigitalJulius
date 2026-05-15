"""Tiny secret vault for API keys.

Stored at ~/.digitaljulius/secrets.json. On POSIX we chmod 0o600 so other
users can't read it. On Windows the file lives under the user profile, where
NTFS ACLs already restrict access by default.

Never log secret values; never write them into providers.toml or session log.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from digitaljulius.config import DJ_HOME, ensure_dirs

SECRETS_PATH = DJ_HOME / "secrets.json"


def _load() -> dict[str, str]:
    ensure_dirs()
    if not SECRETS_PATH.exists():
        return {}
    try:
        return json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict[str, str]) -> None:
    ensure_dirs()
    SECRETS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except OSError:
        pass


def get(name: str) -> str | None:
    # Env var wins so users can override per-shell without touching the file.
    env_key = f"DJ_SECRET_{name.upper().replace('-', '_')}"
    if env_key in os.environ:
        return os.environ[env_key]
    return _load().get(name)


def set_(name: str, value: str) -> None:
    data = _load()
    data[name] = value
    _save(data)


def remove(name: str) -> bool:
    data = _load()
    if name not in data:
        return False
    del data[name]
    _save(data)
    return True


def names() -> list[str]:
    return sorted(_load().keys())


def has(name: str) -> bool:
    return get(name) is not None
