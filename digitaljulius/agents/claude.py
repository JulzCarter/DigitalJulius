from __future__ import annotations

import os
from pathlib import Path

from digitaljulius.agents.base import AgentAdapter


class ClaudeAdapter(AgentAdapter):
    name = "claude"
    command = "claude"

    def credentials_path(self) -> Path:
        # Claude Code stores creds in different places depending on platform.
        # On Windows the OneDrive-redirected home applies; we check both.
        home = Path(os.path.expanduser("~"))
        candidates = [
            home / ".claude" / ".credentials.json",
            home / ".claude.json",                     # the global state file
            home / ".config" / "claude" / "credentials.json",
        ]
        for c in candidates:
            if c.exists() and c.stat().st_size > 0:
                return c
        return candidates[1]  # .claude.json is the most likely indicator on this machine

    def build_argv(self, prompt: str, model: str, yolo: bool, cwd: Path) -> list[str]:
        argv = [self.command, "-p", prompt]
        if model:
            argv.extend(["--model", model])
        if yolo:
            argv.append("--dangerously-skip-permissions")
        return argv
