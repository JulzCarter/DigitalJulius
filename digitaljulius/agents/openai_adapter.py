"""OpenAI Python SDK adapter — secondary boss after Claude.

Uses the OpenAI SDK directly (not subprocess) because there is no
"OpenAI Code" CLI we want to shell out to for plain chat completion. For
the deep-coding CLI use `agents/codex.py` instead.

Authentication: looks up the API key via `secrets.get("OPENAI_API_KEY")`
or the standard `OPENAI_API_KEY` environment variable. Either works.

Promotion rule: this adapter is registered as the cross-agent escalation
target when the Claude adapter reports QUOTA_EXCEEDED — see config.py
routing tables and orchestrator's `_escalate_to_openai`.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from digitaljulius.agents.base import AgentAdapter, AgentResponse
from digitaljulius import secrets
from digitaljulius.log import get_logger

log = get_logger(__name__)

# Highest-tier first. The orchestrator walks this top-down on quota.
# When Claude credits run out the orchestrator immediately jumps to the
# top of this chain (gpt-5) for the user-facing answer.
TOP_MODEL = "gpt-5"
DEFAULT_CHAIN = [
    "gpt-5",
    "gpt-5-pro",
    "o3-pro",
    "gpt-5-mini",
    "gpt-4.1",
    "gpt-4o",
    "gpt-4o-mini",
]


class OpenAIAdapter(AgentAdapter):
    name = "openai"
    command = "openai"  # nominal — we never shell out

    def is_installed(self) -> bool:
        try:
            import openai  # noqa: F401
            return True
        except ImportError:
            return False

    def credentials_path(self) -> Path:
        # We don't write a credentials file — the secret lives in
        # ~/.digitaljulius/secrets.json or the OPENAI_API_KEY env var.
        # Return a sentinel path that exists when we have a usable key.
        from digitaljulius.config import DJ_HOME
        return DJ_HOME / "secrets.json"

    def is_authenticated(self) -> bool:
        return bool(secrets.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"))

    def build_argv(self, prompt: str, model: str, yolo: bool, cwd: Path) -> list[str]:
        # Unused — we override run().
        return []

    def run(
        self,
        prompt: str,
        model: str,
        yolo: bool = True,
        cwd: Path | None = None,
        timeout: int = 300,
    ) -> AgentResponse:
        t0 = time.time()
        api_key = secrets.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            log.warning("openai: no API key configured")
            return AgentResponse(
                agent=self.name, model=model, ok=False, text="",
                stderr="OPENAI_API_KEY not set — run /openai set-key",
                returncode=1, duration_s=time.time() - t0,
            )
        try:
            from openai import OpenAI
        except ImportError as e:
            log.error("openai: SDK not installed (%s)", e)
            return AgentResponse(
                agent=self.name, model=model, ok=False, text="",
                stderr=f"openai package missing: {e} — pip install openai",
                returncode=127, duration_s=time.time() - t0,
            )

        log.info("openai.run model=%s prompt_chars=%d", model, len(prompt))
        try:
            client = OpenAI(api_key=api_key, timeout=timeout)
            # Reasoning models (o-series, gpt-5-pro) prefer the Responses API
            # and need a different call shape — fall through to chat completions
            # for the standard models. Both endpoints accept gpt-5 family.
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
            text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
            dur = time.time() - t0
            log.info("openai.done model=%s tokens_out=%s dur=%.1fs",
                     model,
                     getattr(getattr(resp, "usage", None), "completion_tokens", "?"),
                     dur)
            return AgentResponse(
                agent=self.name, model=model, ok=bool(text),
                text=text, stderr="", returncode=0, duration_s=dur,
            )
        except Exception as e:  # SDK raises typed errors; treat all as failure
            dur = time.time() - t0
            err_text = f"{type(e).__name__}: {e}"
            log.warning("openai.fail model=%s err=%s", model, err_text[:200])
            return AgentResponse(
                agent=self.name, model=model, ok=False, text="",
                stderr=err_text, returncode=1, duration_s=dur,
            )

    def is_quota_error(self, response: AgentResponse) -> bool:
        err = (response.stderr + " " + response.text).lower()
        patterns = [
            r"rate.?limit",
            r"quota",
            r"insufficient[_ ]quota",
            r"insufficient_funds",
            r"billing",
            r"\b429\b",
            r"\b402\b",
            r"exceeded your current quota",
            r"hard.?limit",
            r"capacity",
        ]
        return any(re.search(p, err) for p in patterns)
