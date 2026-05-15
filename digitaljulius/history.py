"""Conversation memory.

The orchestrator runs each prompt independently — but for follow-up prompts
the user expects DigitalJulius to remember what just happened. We synthesise
a compact recap of the last N turns and inject it ahead of the user's prompt.
"""
from __future__ import annotations

from typing import Iterable


def build_history_context(
    turns: Iterable,
    max_turns: int = 4,
    max_chars: int = 3500,
) -> str:
    """Format recent turns as a short transcript. `turns` is an iterable of
    SessionTurn (from digitaljulius.state). Most-recent-last."""
    items = list(turns)
    if not items:
        return ""
    recent = items[-max_turns:]
    lines = ["## Recent conversation (for follow-up context)"]
    for t in recent:
        prompt_snip = (t.prompt or "").strip().replace("\n", " ")
        if len(prompt_snip) > 240:
            prompt_snip = prompt_snip[:240] + "…"
        reply_snip = (t.final_text or "").strip()
        if len(reply_snip) > 600:
            reply_snip = reply_snip[:600] + "…"
        lines.append(f"\n**user:** {prompt_snip}")
        if reply_snip:
            lines.append(f"**assistant ({t.chosen_agent or 'dj'}):** {reply_snip}")
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…(history truncated)"
    body += "\n## End recent conversation"
    return body
