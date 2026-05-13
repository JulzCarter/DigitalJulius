"""Classify an incoming prompt into one of four complexity tiers.

Uses the configured cheap-classifier agent (Gemini Flash by default). Falls
back to a heuristic when the classifier agent is unavailable.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from digitaljulius.agents.registry import get_agent
from digitaljulius.budget import record_call


class Tier(str, Enum):
    SIMPLE = "SIMPLE"
    MODERATE = "MODERATE"
    COMPLEX = "COMPLEX"
    CRITICAL = "CRITICAL"


@dataclass
class Classification:
    tier: Tier
    reason: str
    suggested_tags: list[str]   # ["refactor", "long_context", ...]
    raw: str = ""


CLASSIFY_PROMPT = """Classify the following user prompt into ONE tier. Respond ONLY with a valid JSON object in this exact shape:

{
  "tier": "SIMPLE" | "MODERATE" | "COMPLEX" | "CRITICAL",
  "reason": "<one short sentence>",
  "tags": ["architecture"|"refactor"|"quick_edit"|"long_context"|"web_search"|"math"|"default", ...]
}

Tier guidance:
- SIMPLE:    one-shot question or trivial edit. Single agent, cheapest model.
- MODERATE:  multi-step task, focused scope. Single agent + light review.
- COMPLEX:   multi-file/multi-component, ambiguity, design choices. Two agents + synthesis.
- CRITICAL:  irreversible, security-sensitive, large architectural change, or user explicitly says "important/production".

User prompt:
---
{prompt}
---

Output JSON only. No prose."""


HEURISTIC_KEYWORDS = {
    Tier.CRITICAL: [
        "production", "prod ", "deploy", "release", "security",
        "auth ", "migration", "drop ", "rm -rf", "force push",
        "credentials", "secret", "irreversible",
    ],
    Tier.COMPLEX: [
        "refactor", "architecture", "design", "multiple files",
        "across the codebase", "rewrite", "migrate",
    ],
    Tier.MODERATE: [
        "implement", "build", "add ", "create ", "write a",
        "fix the", "debug",
    ],
}


def _heuristic(prompt: str) -> Classification:
    p = prompt.lower()
    for tier in (Tier.CRITICAL, Tier.COMPLEX, Tier.MODERATE):
        for kw in HEURISTIC_KEYWORDS[tier]:
            if kw in p:
                return Classification(
                    tier=tier,
                    reason=f"heuristic match: {kw!r}",
                    suggested_tags=["default"],
                )
    return Classification(
        tier=Tier.SIMPLE, reason="heuristic: no complexity markers", suggested_tags=["default"]
    )


def classify(prompt: str, cfg: dict, cwd: Path | None = None) -> Classification:
    classifier_cfg = cfg.get("classifier", {})
    agent_name = classifier_cfg.get("agent", "gemini")
    model = classifier_cfg.get("model", "gemini-2.5-flash")

    try:
        adapter = get_agent(agent_name)
    except KeyError:
        return _heuristic(prompt)

    if not (adapter.is_installed() and adapter.is_authenticated()):
        return _heuristic(prompt)

    full_prompt = CLASSIFY_PROMPT.replace("{prompt}", prompt)
    resp = adapter.run(
        full_prompt, model=model, yolo=True, cwd=cwd, timeout=60
    )
    record_call(agent_name, model)

    if not resp.ok or not resp.text:
        return _heuristic(prompt)

    parsed = _extract_json(resp.text)
    if not parsed:
        return _heuristic(prompt)

    try:
        tier = Tier(parsed.get("tier", "SIMPLE").upper())
    except ValueError:
        tier = Tier.MODERATE
    return Classification(
        tier=tier,
        reason=parsed.get("reason") or "classifier",
        suggested_tags=parsed.get("tags") or ["default"],
        raw=resp.text,
    )


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of `text`, tolerating code fences."""
    text = text.strip()
    # strip ```json ... ``` fences
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
