from __future__ import annotations

import os
from pathlib import Path

from digitaljulius.agents.base import AgentAdapter


class QwenAdapter(AgentAdapter):
    name = "qwen"
    command = "qwen"

    def credentials_path(self) -> Path:
        return Path(os.path.expanduser("~")) / ".qwen" / "oauth_creds.json"

    def build_argv(self, prompt: str, model: str, yolo: bool, cwd: Path) -> list[str]:
        # Qwen Code is a Gemini-CLI fork — flags match.
        argv = [self.command]
        if model:
            argv.extend(["--model", model])
        if yolo:
            argv.append("--yolo")
        # Positional prompt is the documented headless mode in Qwen Code.
        argv.append(prompt)
        return argv
