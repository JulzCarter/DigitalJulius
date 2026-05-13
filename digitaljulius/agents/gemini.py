from __future__ import annotations

import os
from pathlib import Path

from digitaljulius.agents.base import AgentAdapter


class GeminiAdapter(AgentAdapter):
    name = "gemini"
    command = "gemini"

    def credentials_path(self) -> Path:
        return Path(os.path.expanduser("~")) / ".gemini" / "oauth_creds.json"

    def build_argv(self, prompt: str, model: str, yolo: bool, cwd: Path) -> list[str]:
        argv = [self.command, "-p", prompt]
        if model:
            argv.extend(["--model", model])
        if yolo:
            argv.append("--yolo")
        return argv
