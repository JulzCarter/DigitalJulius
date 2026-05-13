"""Unified knowledge center.

DigitalJulius accumulates lessons across every session:
    LEARNED.md         — distilled insights about what worked / what didn't
    PREFERENCES.md     — user style preferences inferred over time
    ROUTING_INSIGHTS.md — which agent excels at which task type
    FAILURES.md        — patterns to avoid (incl. fix the underlying cause)

Knowledge lives at ~/.digitaljulius/knowledge/ and is injected into every
non-SIMPLE prompt as additional context so the orchestrator self-improves.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from digitaljulius.agents.registry import get_agent
from digitaljulius.budget import record_call
from digitaljulius.config import DJ_HOME, ensure_dirs

KB_DIR = DJ_HOME / "knowledge"
KB_FILES = {
    "learned": KB_DIR / "LEARNED.md",
    "preferences": KB_DIR / "PREFERENCES.md",
    "routing": KB_DIR / "ROUTING_INSIGHTS.md",
    "failures": KB_DIR / "FAILURES.md",
}


def ensure_kb() -> None:
    ensure_dirs()
    KB_DIR.mkdir(parents=True, exist_ok=True)
    for kind, path in KB_FILES.items():
        if not path.exists():
            path.write_text(
                f"# {kind.upper()}\n\n"
                f"<!-- DigitalJulius writes one bullet per entry. Do not edit format. -->\n\n",
                encoding="utf-8",
            )


@dataclass
class KnowledgeEntry:
    kind: str          # learned | preferences | routing | failures
    ts: str
    text: str


def _append(kind: str, text: str) -> KnowledgeEntry:
    ensure_kb()
    path = KB_FILES[kind]
    ts = datetime.now().isoformat(timespec="seconds")
    text = text.strip()
    if not text:
        return KnowledgeEntry(kind=kind, ts=ts, text="")
    line = f"- `{ts}` {text}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
    return KnowledgeEntry(kind=kind, ts=ts, text=text)


def learn(text: str, kind: str = "learned") -> KnowledgeEntry:
    """User-facing /learn — capture a lesson explicitly."""
    if kind not in KB_FILES:
        kind = "learned"
    return _append(kind, text)


def all_entries() -> dict[str, list[str]]:
    """Return raw bullet lines per category for /knowledge rendering."""
    ensure_kb()
    out: dict[str, list[str]] = {}
    for kind, path in KB_FILES.items():
        lines = [
            ln.rstrip("\n")
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.startswith("- ")
        ]
        out[kind] = lines
    return out


def forget(needle: str) -> int:
    """Remove every bullet whose text contains `needle` (case-insensitive)."""
    ensure_kb()
    removed = 0
    needle_lc = needle.lower()
    for kind, path in KB_FILES.items():
        original = path.read_text(encoding="utf-8")
        kept_lines = []
        for line in original.splitlines(keepends=True):
            if line.startswith("- ") and needle_lc in line.lower():
                removed += 1
                continue
            kept_lines.append(line)
        path.write_text("".join(kept_lines), encoding="utf-8")
    return removed


def context_for_prompt(max_chars: int = 4000) -> str:
    """Build a compact context block to inject into the orchestrator's
    prompts. Most-recent-first, capped to keep token cost sane."""
    ensure_kb()
    blocks: list[str] = []
    headings = {
        "preferences": "User preferences (apply unless overridden):",
        "learned":     "Lessons from prior sessions:",
        "routing":     "Routing insights (which agent excels where):",
        "failures":    "Known failure patterns to avoid:",
    }
    entries = all_entries()
    for kind, header in headings.items():
        bullets = entries.get(kind, [])
        if not bullets:
            continue
        # Most recent 8 per category, newest first.
        recent = bullets[-8:][::-1]
        blocks.append(header + "\n" + "\n".join(recent))
    body = "\n\n".join(blocks)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…(truncated)"
    if not body:
        return ""
    return (
        "## DigitalJulius accumulated knowledge\n"
        f"{body}\n"
        "## End knowledge — apply where relevant."
    )


# ---------------------------------------------------------------------------
# Auto-distillation: ask a cheap model to extract a single learning from the
# last turn. Skipped for SIMPLE tier to keep cost down.
# ---------------------------------------------------------------------------

DISTILL_PROMPT = """You watch a multi-agent orchestrator (DigitalJulius) run.
After each turn, decide if there is a single, durable lesson worth saving for
future sessions. MOST turns yield no lesson — be strict, return empty.

Save a lesson only if at least one is true:
- The user expressed a preference that should persist (style, tone, tools).
- One agent clearly outperformed the others for this task type.
- A common failure pattern was observed (and how to avoid it).

Respond ONLY with JSON:
{
  "save": true | false,
  "kind": "learned" | "preferences" | "routing" | "failures",
  "text": "<one short sentence, no preamble>"
}

Turn:
  user prompt: {prompt}
  tier: {tier}
  chosen agent: {agent} ({model})
  approved: {approved}
  output (first 1200 chars): {output}

Output JSON only."""


def auto_distill(
    prompt: str,
    tier: str,
    chosen_agent: str,
    chosen_model: str,
    output: str,
    approved: bool | None,
    cfg: dict,
    cwd: Path | None = None,
) -> KnowledgeEntry | None:
    """Optionally extract a learning from the last turn using the classifier
    agent (cheap fast model). Returns the saved entry or None."""
    if tier == "SIMPLE":
        return None
    if not output:
        return None

    classifier_cfg = cfg.get("classifier", {})
    agent_name = classifier_cfg.get("agent", "gemini")
    model = classifier_cfg.get("model", "gemini-2.5-flash")
    try:
        adapter = get_agent(agent_name)
    except KeyError:
        return None
    if not (adapter.is_installed() and adapter.is_authenticated()):
        return None

    full = (
        DISTILL_PROMPT
        .replace("{prompt}", prompt[:600])
        .replace("{tier}", tier)
        .replace("{agent}", chosen_agent or "-")
        .replace("{model}", chosen_model or "-")
        .replace("{approved}", "yes" if approved else "no" if approved is False else "n/a")
        .replace("{output}", output[:1200])
    )
    resp = adapter.run(full, model=model, yolo=True, cwd=cwd, timeout=60)
    record_call(agent_name, model)
    if not resp.ok or not resp.text:
        return None

    parsed = _extract_json(resp.text)
    if not parsed or not parsed.get("save"):
        return None
    kind = parsed.get("kind", "learned")
    text = (parsed.get("text") or "").strip()
    if not text:
        return None
    return _append(kind if kind in KB_FILES else "learned", text)


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        candidate = m.group(0) if m else text
    try:
        return json.loads(candidate)
    except Exception:
        return None
