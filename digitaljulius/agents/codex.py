"""OpenAI Codex CLI adapter — deep-coding worker.

Codex CLI is OpenAI's terminal coding agent (`codex` on PATH). We treat it
like Claude Code / Gemini CLI: subprocess + headless one-shot.

Routing role: when the complexity classifier returns COMPLEX/CRITICAL with
deep-coding tags, the orchestrator routes here so Codex can spawn its own
sub-agents internally for parallel work.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from digitaljulius.agents.base import AgentAdapter, AgentResponse


TOP_MODEL = "gpt-5-codex"
DEFAULT_CHAIN = [
    "gpt-5-codex",
    "gpt-5",
    "gpt-5-mini",
    "o3-pro",
    "gpt-4.1",
]


class CodexAdapter(AgentAdapter):
    name = "codex"
    command = "codex"

    def is_installed(self) -> bool:
        # Codex CLI ships as `codex` on PATH; on Windows it may be a .cmd shim.
        if shutil.which(self.command) is not None:
            return True
        for ext in (".cmd", ".ps1", ".exe"):
            if shutil.which(self.command + ext) is not None:
                return True
        return False

    def credentials_path(self) -> Path:
        # Codex CLI stores auth under ~/.codex (login via `codex login`).
        home = Path(os.path.expanduser("~"))
        candidates = [
            home / ".codex" / "auth.json",
            home / ".codex" / "credentials.json",
            home / ".config" / "codex" / "auth.json",
        ]
        for c in candidates:
            if c.exists() and c.stat().st_size > 0:
                return c
        return candidates[0]

    def is_authenticated(self) -> bool:
        # Either the local creds file exists OR an OPENAI_API_KEY is set —
        # Codex CLI accepts both.
        if super().is_authenticated():
            return True
        from digitaljulius import secrets
        return bool(secrets.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"))

    def build_argv(self, prompt: str, model: str, yolo: bool, cwd: Path) -> list[str]:
        # `codex exec` is the headless one-shot mode (vs interactive `codex`).
        # --skip-git-repo-check lets it run outside a git repo.
        # Sandbox is workspace-write so Codex can edit files in cwd.
        argv = [self.command, "exec", "--skip-git-repo-check"]
        if model:
            argv.extend(["--model", model])
        if yolo:
            argv.extend(["--sandbox", "workspace-write", "--ask-for-approval", "never"])
        argv.append(prompt)
        return argv

    def is_quota_error(self, response: AgentResponse) -> bool:
        err = (response.stderr + " " + response.text).lower()
        patterns = [
            r"rate.?limit",
            r"quota",
            r"insufficient[_ ]quota",
            r"billing",
            r"\b429\b",
            r"\b402\b",
            r"exceeded your current quota",
            r"weekly limit",
            r"plus.?limit",
            r"pro.?limit",
        ]
        return any(re.search(p, err) for p in patterns)
