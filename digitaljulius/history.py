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
    chunks: list[str] = []
    for t in recent:
        prompt_snip = (t.prompt or "").strip().replace("\n", " ")
        if len(prompt_snip) > 240:
            prompt_snip = prompt_snip[:240] + "…"
        reply_snip = (t.final_text or "").strip()
        if len(reply_snip) > 600:
            reply_snip = reply_snip[:600] + "…"
        chunk_lines = [f"**user:** {prompt_snip}"]
        if reply_snip:
            chunk_lines.append(f"**assistant ({t.chosen_agent or 'dj'}):** {reply_snip}")
        chunks.append("\n".join(chunk_lines))

    selected_newest_first: list[str] = []
    used = 0
    for chunk in reversed(chunks):
        separator_len = 2 if selected_newest_first else 0
        next_len = separator_len + len(chunk)
        if selected_newest_first and used + next_len > max_chars:
            break
        selected_newest_first.append(chunk)
        used += next_len
        if used >= max_chars:
            break

    selected = list(reversed(selected_newest_first))
    body = "## Recent conversation (for follow-up context)"
    if selected:
        body += "\n" + "\n\n".join(selected)
    if len(selected) < len(chunks):
        body += "\n…(history truncated)"
    body += "\n## End recent conversation"
    return body
