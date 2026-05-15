"""Per-project context auto-loading.

When `dj` launches in a directory we look for project-level memory files and
keep their contents around to prepend to every non-trivial prompt:

    .digitaljulius/PROJECT.md        (DJ's own file, written by /init)
    .shared-agent-context/CURRENT_CONTEXT.md
    .shared-agent-context/DECISIONS.md
    CLAUDE.md / AGENTS.md / GEMINI.md   (root-level agent guides)

Everything is concatenated, capped, and tagged so the agent knows it's
context-of-the-repo, not the user's latest prompt.
"""
from __future__ import annotations

from pathlib import Path

CANDIDATES = [
    ".digitaljulius/PROJECT.md",
    ".shared-agent-context/CURRENT_CONTEXT.md",
    ".shared-agent-context/DECISIONS.md",
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
]


def collect_project_context(cwd: Path, max_chars: int = 4000) -> str:
    """Read whatever project memory files exist and return a single tagged
    block. Returns "" when nothing is found."""
    sections: list[str] = []
    remaining = max_chars
    for rel in CANDIDATES:
        if remaining <= 0:
            break
        path = cwd / rel
        if not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            continue
        if not body:
            continue
        # Per-file cap so one huge file doesn't crowd out the rest.
        per_file_cap = max(800, remaining // 2)
        per_file_cap = min(per_file_cap, remaining)
        if len(body) > per_file_cap:
            marker = "\n…(file truncated)"
            if per_file_cap > len(marker):
                body = body[: per_file_cap - len(marker)] + marker
            else:
                body = body[:per_file_cap]
        sections.append(f"### {rel}\n{body}")
        remaining -= len(body)
    if not sections:
        return ""
    return (
        "## Project context (auto-loaded)\n"
        + "\n\n".join(sections)
        + "\n## End project context"
    )
