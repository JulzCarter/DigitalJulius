"""GitHub Models adapter — talks to `gh models run` via the gh CLI.

Auth is the user's existing GitHub OAuth (`gh auth login`). Inference is free
for individual GitHub accounts subject to per-model daily request caps. The
catalogue covers OpenAI, Llama, Mistral, Phi, DeepSeek and more — perfect as
the always-on fallback when Claude credits run out.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from digitaljulius.agents.base import AgentAdapter, AgentResponse


_EXT_CHECK_CACHE: dict[str, bool] = {}


def _has_models_extension() -> bool:
    """Cheap memoised check that the `gh models` extension is installed."""
    if "models" in _EXT_CHECK_CACHE:
        return _EXT_CHECK_CACHE["models"]
    try:
        res = subprocess.run(
            ["gh", "extension", "list"],
            capture_output=True, text=True, timeout=5,
        )
        ok = res.returncode == 0 and "gh-models" in (res.stdout or "")
    except Exception:
        ok = False
    _EXT_CHECK_CACHE["models"] = ok
    return ok


class GitHubModelsAdapter(AgentAdapter):
    name = "github"
    command = "gh"

    def is_installed(self) -> bool:
        if shutil.which(self.command) is None:
            return False
        return _has_models_extension()

    def credentials_path(self) -> Path:
        # gh stores tokens in hosts.yml on Linux/macOS and in the Windows
        # credential keyring on Windows. We use the file as a soft proof of
        # auth but is_authenticated() below is the source of truth.
        home = Path(os.path.expanduser("~"))
        for c in [
            home / ".config" / "gh" / "hosts.yml",
            home / "AppData" / "Roaming" / "GitHub CLI" / "hosts.yml",
        ]:
            if c.exists():
                return c
        return home / ".config" / "gh" / "hosts.yml"

    def is_authenticated(self) -> bool:
        try:
            res = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True, text=True, timeout=5,
            )
            return res.returncode == 0
        except Exception:
            return False

    def build_argv(self, prompt: str, model: str, yolo: bool, cwd: Path) -> list[str]:
        # Prompt is piped over stdin in run() instead — argv form breaks on
        # long prompts and shell-special characters on Windows.
        del prompt, yolo, cwd
        return [self.command, "models", "run", model]

    def run(
        self,
        prompt: str,
        model: str,
        yolo: bool = True,
        cwd: Path | None = None,
        timeout: int = 300,
    ) -> AgentResponse:
        del yolo  # gh models has no permission gating
        cwd = cwd or Path.cwd()
        argv = self.build_argv(prompt, model, yolo=True, cwd=cwd)
        t0 = time.time()
        try:
            result = subprocess.run(
                argv,
                cwd=str(cwd),
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return AgentResponse(
                agent=self.name,
                model=model,
                ok=result.returncode == 0 and bool((result.stdout or "").strip()),
                text=(result.stdout or "").strip(),
                stderr=(result.stderr or "").strip(),
                returncode=result.returncode,
                duration_s=time.time() - t0,
            )
        except subprocess.TimeoutExpired:
            return AgentResponse(
                agent=self.name, model=model, ok=False, text="",
                stderr=f"timeout after {timeout}s",
                returncode=124, duration_s=time.time() - t0,
            )
        except FileNotFoundError as e:
            return AgentResponse(
                agent=self.name, model=model, ok=False, text="",
                stderr=f"command not found: {e}",
                returncode=127, duration_s=time.time() - t0,
            )

    def is_quota_error(self, response: AgentResponse) -> bool:
        err = (response.stderr + " " + response.text).lower()
        patterns = [
            r"rate limit",
            r"rate-limit",
            r"quota exceeded",
            r"too many requests",
            r"\b429\b",
            r"daily request limit",
            r"requests per day",
            r"throttled",
        ]
        return any(re.search(p, err) for p in patterns)
