"""First-run auth probe and walk-through."""
from __future__ import annotations

import json
import shutil
import subprocess
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


def interactive_login(agent_name: str) -> bool:
    """Spawn the agent CLI attached to the user's terminal so they can complete
    its OAuth flow in place. Returns True if `is_authenticated()` flips to True
    after the user exits the child process."""
    adapter = AGENTS.get(agent_name)
    if adapter is None:
        return False
    if not adapter.is_installed():
        return False
    cmd_path = shutil.which(adapter.command) or adapter.command
    try:
        # No capture_output / no input: stdin/stdout/stderr inherit the parent
        # terminal so the user types into the CLI directly. The agent prints
        # its OAuth URL, opens a browser, and drops into its TUI when done.
        subprocess.run([cmd_path], check=False)
    except KeyboardInterrupt:
        # User pressed Ctrl+C inside the child — that's a normal way to exit
        # those TUIs. Fall through to the auth re-check.
        pass
    except FileNotFoundError:
        return False
    return adapter.is_authenticated()


def instructions_for(agent: str) -> str:
    """One-line tagline describing what the OAuth flow will do."""
    return {
        "claude":
            "Sign in to your Anthropic account (free if you have Claude Code).",
        "gemini":
            "Sign in to a personal Google account (free tier, no credit card).",
        "qwen":
            "Sign in to Alibaba Cloud (free tier, no credit card).",
    }.get(agent, f"Run the `{agent}` OAuth flow.")
