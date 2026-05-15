"""Plan-then-execute mode.

Before kicking off COMPLEX/CRITICAL work (or whenever the user explicitly
runs `/plan`), the orchestrator drafts a structured plan via the planning
role (top-tier only, never silent downgrade — see core_directives). The
plan is shown to the user with an expand toggle (default collapsed: just
the summary + step count). User approves, edits, or aborts before any
worker agent runs.

Returned `Plan` objects are passed back into the orchestrator so the
worker prompt is enriched with the agreed plan — agents follow it instead
of re-deriving their own.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from digitaljulius.agents.base import AgentResponse
from digitaljulius.log import get_logger
from digitaljulius.roles import PlanningChoiceFn, resilient_role_call

log = get_logger(__name__)


@dataclass
class Plan:
    summary: str = ""
    steps: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)  # files/URLs the user should expect
    raw: str = ""
    approved: bool = False
    edited_by_user: bool = False

    def is_empty(self) -> bool:
        return not (self.summary or self.steps)


PLAN_PROMPT = """You are the plan-drafter for an orchestrator that routes work
across Claude Code, OpenAI (gpt-5), Codex CLI, Gemini, and GitHub Models.

The user prompt is below. Produce a STRUCTURED, EXECUTABLE plan — not a
discussion. The orchestrator will hand this plan to worker agents, who will
EXECUTE the steps (edit files, run commands, build artifacts). Do not write
anything that requires the user to perform manual steps unless absolutely
necessary.

Respond ONLY with valid JSON in this exact shape:

{
  "summary": "<one sentence — what this turn produces>",
  "steps": ["<imperative step>", "<step>", ...],
  "risks": ["<thing that could go wrong>", ...],
  "artifacts": ["<file path or URL the user will see when this is done>", ...]
}

Rules:
- Steps must be IMPERATIVE and EXECUTABLE by an AI agent. "Run `npm install`",
  "Edit src/foo.py:42 to use ...", "Create file X.md with ..." — not "consider X".
- Keep steps to <= 8. If the task needs more, summarise the later phases.
- `artifacts` must be concrete: file paths, URLs, commit hashes the user can inspect.
- `risks` are real failure modes (auth, missing deps, irreversible ops) — not generic warnings.

User prompt:
---
{prompt}
---

Output JSON only."""


def draft_plan(
    user_prompt: str,
    cfg: dict,
    cwd: Path | None = None,
    confirm_planning: PlanningChoiceFn | None = None,
) -> Plan | None:
    """Ask the planning role to draft a structured plan. Top-tier only."""
    plan_cfg = cfg.get("approver", {"agent": "claude", "model": "opus"})
    prompt = PLAN_PROMPT.replace("{prompt}", user_prompt)
    log.info("planning.draft_start prompt_chars=%d", len(user_prompt))
    resp = resilient_role_call(
        plan_cfg, prompt, cfg, cwd=cwd, timeout=120,
        planning=True, confirm_planning=confirm_planning,
    )
    if resp is None or not resp.ok or not resp.text:
        log.warning("planning.draft_failed")
        return None

    parsed = _extract_json(resp.text)
    if not parsed:
        log.warning("planning.draft_unparseable")
        return None

    plan = Plan(
        summary=parsed.get("summary", "").strip(),
        steps=[s.strip() for s in parsed.get("steps", []) if s and s.strip()],
        risks=[r.strip() for r in parsed.get("risks", []) if r and r.strip()],
        artifacts=[a.strip() for a in parsed.get("artifacts", []) if a and a.strip()],
        raw=resp.text,
    )
    if not plan.steps:
        log.warning("planning.draft_empty_steps")
        return None
    log.info("planning.draft_done steps=%d risks=%d artifacts=%d",
             len(plan.steps), len(plan.risks), len(plan.artifacts))
    return plan


def plan_to_worker_prefix(plan: Plan) -> str:
    """Render an approved plan into a prefix block for worker agents."""
    if plan.is_empty():
        return ""
    lines = ["[APPROVED PLAN — execute this plan exactly; do not re-derive]"]
    lines.append(f"Goal: {plan.summary}")
    if plan.steps:
        lines.append("Steps:")
        for i, s in enumerate(plan.steps, 1):
            lines.append(f"  {i}. {s}")
    if plan.artifacts:
        lines.append("Required artifacts to surface to user:")
        for a in plan.artifacts:
            lines.append(f"  - {a}")
    if plan.risks:
        lines.append("Known risks to mitigate:")
        for r in plan.risks:
            lines.append(f"  - {r}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        candidate = m.group(0) if m else text
    try:
        return json.loads(candidate)
    except Exception:
        return None
