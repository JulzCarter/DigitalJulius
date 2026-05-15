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
from digitaljulius.budget import best_available_model, exhaust_model, record_call
from digitaljulius.roles import SESSION_SKIP, _is_soft_skip, _looks_like_quota, mark_session_skip


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


# Meta-modification intent: anything that asks DigitalJulius itself to change
# its own behaviour (logging, display, routing, prompts, output format…) must
# go to claude — only claude has the source-map context. Detecting these here
# saves the 10s classifier round-trip and prevents Gemini from hallucinating
# the task into "build BrunchBuddy" because it found a folder.
META_PATTERNS = [
    r"\byour (process|log|logs|logging|display|reporter|orchestrator|prompt|router|tier|cli|repl|ui|output|self|code|routing|classifier|behavi(or|our)|setup|config|wizard)\b",
    r"\bthis (cli|orchestrator|repl|tool|app)\b",
    r"\bdigitaljulius\b",
    r"\b(dj|the)\s+(cli|orchestrator|repl|tool)\b",
    r"\bfix (your|the) (way|process|logging|display|output|ui|reporter)\b",
    r"\b(make|change|update|modify|tweak) (you|yourself|how you|the way you)\b",
    r"\bself[- ]modify\b",
]
META_RE = re.compile("|".join(META_PATTERNS), re.IGNORECASE)


def _looks_like_meta(prompt: str) -> bool:
    return bool(META_RE.search(prompt))


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
    """Classify with the configured fast classifier ONLY.

    Critical: this runs on the hot path before every prompt. We do NOT walk the
    classifier agent's fallback chain (each Gemini call is ~10-15s, so walking
    a 5-model chain stalls the CLI for a minute). One shot at the configured
    model; on any failure (quota / soft-skip / parse error / timeout) we fall
    straight to the keyword heuristic — which is instant and good enough to
    route prompts.
    """
    # Pre-classifier short-circuit: meta-modification requests skip the
    # classifier entirely and pin to claude. Saves the classifier round-trip
    # AND prevents Gemini/Codex from misreading "fix your log display" as
    # "build a logging app."
    if _looks_like_meta(prompt):
        return Classification(
            tier=Tier.SIMPLE,
            reason="meta-modification request — pinned to claude (boss owns DigitalJulius source)",
            suggested_tags=["self_modify"],
            raw="(meta short-circuit)",
        )

    classifier_cfg = cfg.get("classifier", {"agent": "gemini", "model": "gemini-2.5-flash"})
    agent_name = classifier_cfg.get("agent", "gemini")
    model = classifier_cfg.get("model", "gemini-2.5-flash")

    if agent_name in SESSION_SKIP:
        return _heuristic(prompt)

    try:
        adapter = get_agent(agent_name)
    except KeyError:
        return _heuristic(prompt)

    if not (adapter.is_installed() and adapter.is_authenticated()):
        return _heuristic(prompt)

    # If the configured model is already over today's switch threshold, jump
    # to the agent's next still-usable model instead of burning a request.
    if not _model_usable(cfg, agent_name, model):
        nxt = best_available_model(cfg, agent_name)
        if not nxt:
            return _heuristic(prompt)
        model = nxt

    full_prompt = CLASSIFY_PROMPT.replace("{prompt}", prompt)
    resp = adapter.run(full_prompt, model=model, yolo=True, cwd=cwd, timeout=20)

    if not resp.ok or not resp.text:
        if _looks_like_quota(resp, adapter):
            exhaust_model(agent_name, model)
        elif _is_soft_skip(resp):
            mark_session_skip(agent_name, "environment-not-supported")
        return _heuristic(prompt)

    record_call(agent_name, model)
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


def _model_usable(cfg: dict, agent: str, model: str) -> bool:
    from digitaljulius.budget import usage_pct
    switch = float(cfg["budget"]["switch_pct"])
    return usage_pct(cfg, agent, model) < switch


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
