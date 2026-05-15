from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from digitaljulius.agents.base import AgentAdapter, AgentResponse


class GeminiAdapter(AgentAdapter):
    name = "gemini"
    command = "gemini"

    def is_installed(self) -> bool:
        return shutil.which(self.command) is not None or shutil.which(self.command + ".ps1") is not None or shutil.which(self.command + ".cmd") is not None

    def credentials_path(self) -> Path:
        return Path(os.path.expanduser("~")) / ".gemini" / "oauth_creds.json"

    def build_argv(self, prompt: str, model: str, yolo: bool, cwd: Path) -> list[str]:
        cmd = shutil.which(self.command) or shutil.which(self.command + ".ps1") or shutil.which(self.command + ".cmd") or self.command
        # --skip-trust trusts the current workspace for this invocation. Without
        # it, gemini refuses to run in untrusted folders and we pay ~10s per
        # prompt for a failure that's purely an environment policy issue.
        argv = [cmd, "--skip-trust", "-p", prompt]
        if model:
            argv.extend(["--model", model])
        if yolo:
            argv.append("--yolo")
        return argv

    def is_quota_error(self, response: AgentResponse) -> bool:
        """Check if the response indicates a credit or quota issue."""
        err = (response.stderr + " " + response.text).lower()
        patterns = [
            r"resource_exhausted",
            r"rate limit",
            r"quota exceeded",
            r"no capacity available",
            r"too many requests",
            # Gemini CLI wording for the per-model daily reset:
            r"terminalquotaerror",
            r"exhausted your capacity",
            r"quota will reset",
            r"daily limit",
        ]
        return any(re.search(p, err) for p in patterns)

