from __future__ import annotations

import os
import re
from pathlib import Path

from digitaljulius.agents.base import AgentAdapter, AgentResponse


class ClaudeAdapter(AgentAdapter):
    name = "claude"
    command = "claude"

    def credentials_path(self) -> Path:
        home = Path(os.path.expanduser("~"))
        candidates = [
            home / ".claude" / ".credentials.json",
            home / ".claude.json",
            home / ".config" / "claude" / "credentials.json",
        ]
        for c in candidates:
            if c.exists() and c.stat().st_size > 0:
                return c
        return candidates[1]

    def build_argv(self, prompt: str, model: str, yolo: bool, cwd: Path) -> list[str]:
        argv = [self.command, "-p", prompt]
        if model:
            argv.extend(["--model", model])
        if yolo:
            argv.append("--dangerously-skip-permissions")
        return argv

    def is_quota_error(self, response: AgentResponse) -> bool:
        """Check if the response indicates a credit or quota issue.

        Detects the full set of Anthropic billing/throttling signals so the
        orchestrator can escalate to OpenAI immediately rather than retrying.
        """
        err = (response.stderr + " " + response.text).lower()
        patterns = [
            r"out of credits",
            r"quota exceeded",
            r"rate.?limit",
            r"insufficient funds",
            r"credit balance is too low",
            r"credit balance",
            r"hit your limit",
            r"reached your monthly",
            r"reached your weekly",
            r"reached your daily",
            r"usage limit",
            r"plan limit",
            r"max plan limit",
            r"\b429\b",
            r"\b402\b",
            r"anthropic.*billing",
            r"please add credits",
            r"upgrade your plan",
        ]
        return any(re.search(p, err) for p in patterns)

