"""Abstract base class for agent adapters."""
from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentResponse:
    agent: str
    model: str
    ok: bool
    text: str
    stderr: str = ""
    returncode: int = 0
    duration_s: float = 0.0


class AgentAdapter(ABC):
    """Shells out to a CLI in headless mode and captures the response."""

    name: str = ""
    command: str = ""

    def __init__(self, command: str | None = None) -> None:
        if command:
            self.command = command

    def is_installed(self) -> bool:
        return shutil.which(self.command) is not None

    @abstractmethod
    def credentials_path(self) -> Path:
        """Return the file we treat as proof of authentication."""

    def is_authenticated(self) -> bool:
        path = self.credentials_path()
        return path.exists() and path.stat().st_size > 0

    @abstractmethod
    def build_argv(self, prompt: str, model: str, yolo: bool, cwd: Path) -> list[str]:
        """Build the CLI invocation for a headless one-shot prompt."""

    def run(
        self,
        prompt: str,
        model: str,
        yolo: bool = True,
        cwd: Path | None = None,
        timeout: int = 300,
    ) -> AgentResponse:
        import time
        cwd = cwd or Path.cwd()
        argv = self.build_argv(prompt, model, yolo, cwd)
        t0 = time.time()
        try:
            result = subprocess.run(
                argv,
                cwd=str(cwd),
                input=None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return AgentResponse(
                agent=self.name,
                model=model,
                ok=result.returncode == 0,
                text=(result.stdout or "").strip(),
                stderr=(result.stderr or "").strip(),
                returncode=result.returncode,
                duration_s=time.time() - t0,
            )
        except subprocess.TimeoutExpired:
            return AgentResponse(
                agent=self.name,
                model=model,
                ok=False,
                text="",
                stderr=f"timeout after {timeout}s",
                returncode=124,
                duration_s=time.time() - t0,
            )
        except FileNotFoundError as e:
            return AgentResponse(
                agent=self.name,
                model=model,
                ok=False,
                text="",
                stderr=f"command not found: {e}",
                returncode=127,
                duration_s=time.time() - t0,
            )
