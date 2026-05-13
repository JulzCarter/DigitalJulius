"""First-run auth probe and walk-through."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

from digitaljulius.agents.registry import AGENTS
from digitaljulius.config import STATE_PATH, ensure_dirs


@dataclass
class AuthStatus:
    agent: str
    installed: bool
    authenticated: bool
    note: str = ""


def probe() -> list[AuthStatus]:
    results: list[AuthStatus] = []
    for name, adapter in AGENTS.items():
        installed = adapter.is_installed()
        authed = installed and adapter.is_authenticated()
        note = ""
        if not installed:
            note = f"`{adapter.command}` not found on PATH"
        elif not authed:
            note = f"no credentials at {adapter.credentials_path()}"
        results.append(
            AuthStatus(agent=name, installed=installed, authenticated=authed, note=note)
        )
    return results


def fully_authenticated(probes: Iterable[AuthStatus]) -> bool:
    return all(p.authenticated for p in probes)


def first_run_completed() -> bool:
    ensure_dirs()
    if not STATE_PATH.exists():
        return False
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(data.get("first_run_completed"))


def mark_first_run_complete() -> None:
    ensure_dirs()
    data: dict = {}
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["first_run_completed"] = True
    STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def instructions_for(agent: str) -> str:
    """User-facing instructions for authenticating a specific agent."""
    return {
        "claude": (
            "Open a new terminal and run `claude` once. It should already be "
            "logged in if you've used Claude Code before; if not, follow its "
            "OAuth prompt to sign into your Anthropic account."
        ),
        "gemini": (
            "Open a new terminal and run `gemini` once. It will open a browser "
            "to sign you into a personal Google account — free tier is granted "
            "automatically, no credit card."
        ),
        "qwen": (
            "Open a new terminal and run `qwen` once. It will open a browser "
            "to sign you into Alibaba Cloud — free tier is granted automatically, "
            "no credit card."
        ),
    }.get(agent, f"Run `{agent}` once and follow its auth prompt.")
