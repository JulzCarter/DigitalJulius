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


class SecretsCorruptError(ValueError):
    """Raised when secrets.json exists but cannot be parsed safely."""


def _load() -> dict[str, str]:
    ensure_dirs()
    try:
        raw = SECRETS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as exc:
        raise SecretsCorruptError(
            "secrets.json is corrupt; back it up and remove it before retrying."
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SecretsCorruptError(
            "secrets.json is corrupt; back it up and remove it before retrying."
        ) from exc


def _save(data: dict[str, str]) -> None:
    ensure_dirs()
    tmp_path = SECRETS_PATH.with_name(f"{SECRETS_PATH.name}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, SECRETS_PATH)
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
