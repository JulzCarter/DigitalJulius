"""Claude Opus serves as both plan-approver (pre-execution) and output-reviewer
(post-execution). Two modes:

  - advisory   : returns a critique but never blocks execution
  - gatekeeper : a "BLOCK" verdict short-circuits and we return the critique
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from digitaljulius.agents.registry import get_agent
from digitaljulius.budget import record_call


@dataclass
class Verdict:
    approved: bool
    critique: str
    suggestions: list[str]
    raw: str = ""


PLAN_PROMPT = """You are the plan-reviewer for an orchestrator that routes work
across Claude Code, Gemini CLI, and Qwen Code. Decide whether the proposed plan
is sound. Respond ONLY with valid JSON:

{
  "approved": true | false,
  "critique": "<one paragraph>",
  "suggestions": ["<bullet>", ...]
}

User goal:
---
{goal}
---

Proposed plan:
---
{plan}
---

Output JSON only."""


OUTPUT_PROMPT = """You are the output-reviewer for an orchestrator. The user
asked something, an agent answered. Verify the answer is correct, complete, and
addresses the actual ask. Respond ONLY with valid JSON:

{
  "approved": true | false,
  "critique": "<one paragraph>",
  "suggestions": ["<bullet>", ...]
}

User prompt:
---
{prompt}
---

Agent ({agent}) response:
---
{response}
---

Output JSON only."""


def _run(prompt: str, cfg: dict, cwd: Path | None = None) -> Verdict:
    approver_cfg = cfg.get("approver", {})
    agent_name = approver_cfg.get("agent", "claude")
    model = approver_cfg.get("model", "opus")

    try:
        adapter = get_agent(agent_name)
    except KeyError:
        return Verdict(approved=True, critique="approver unavailable", suggestions=[])

    if not (adapter.is_installed() and adapter.is_authenticated()):
        return Verdict(approved=True, critique="approver not authenticated", suggestions=[])

    resp = adapter.run(prompt, model=model, yolo=True, cwd=cwd, timeout=120)
    record_call(agent_name, model)

    if not resp.ok or not resp.text:
        return Verdict(approved=True, critique="approver failed to respond", suggestions=[], raw=resp.stderr)

    parsed = _extract_json(resp.text)
    if not parsed:
        return Verdict(approved=True, critique=resp.text[:500], suggestions=[], raw=resp.text)

    return Verdict(
        approved=bool(parsed.get("approved", True)),
        critique=parsed.get("critique", ""),
        suggestions=parsed.get("suggestions", []) or [],
        raw=resp.text,
    )


def review_plan(goal: str, plan: str, cfg: dict, cwd: Path | None = None) -> Verdict:
    prompt = PLAN_PROMPT.replace("{goal}", goal).replace("{plan}", plan)
    return _run(prompt, cfg, cwd)


def review_output(user_prompt: str, agent_name: str, response: str, cfg: dict, cwd: Path | None = None) -> Verdict:
    prompt = (
        OUTPUT_PROMPT
        .replace("{prompt}", user_prompt)
        .replace("{agent}", agent_name)
        .replace("{response}", response[:8000])  # cap to keep token cost sane
    )
    return _run(prompt, cfg, cwd)


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
