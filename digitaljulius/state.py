"""Append-only session log + lightweight per-session state."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from digitaljulius.config import LOG_DIR, ensure_dirs


@dataclass
class SessionTurn:
    ts: str
    prompt: str
    tier: str
    chosen_agent: str
    chosen_model: str
    final_text: str
    approved: bool | None = None
    skipped_reason: str = ""
    extras: dict = field(default_factory=dict)


@dataclass
class Session:
    started: str
    cwd: str
    turns: list[SessionTurn] = field(default_factory=list)

    def log_path(self) -> Path:
        ensure_dirs()
        stamp = self.started.replace(":", "-").replace(" ", "_")
        return LOG_DIR / f"session_{stamp}.jsonl"

    def append(self, turn: SessionTurn) -> None:
        self.turns.append(turn)
        ensure_dirs()
        with self.log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(turn), ensure_ascii=False) + "\n")


def new_session(cwd: Path) -> Session:
    return Session(
        started=datetime.now().isoformat(timespec="seconds"),
        cwd=str(cwd),
    )


def turn_from_runresult(prompt: str, run_result: Any) -> SessionTurn:
    approved: bool | None = None
    if run_result.output_verdict is not None:
        approved = bool(run_result.output_verdict.approved)
    return SessionTurn(
        ts=datetime.now().isoformat(timespec="seconds"),
        prompt=prompt,
        tier=run_result.classification.tier.value,
        chosen_agent=run_result.chosen_agent or "",
        chosen_model=run_result.chosen_model or "",
        final_text=run_result.final_text or "",
        approved=approved,
        skipped_reason=run_result.skipped_reason or "",
        extras={
            "tags": run_result.classification.suggested_tags,
            "responses": [
                {"agent": r.agent, "model": r.model, "ok": r.ok, "dur": r.duration_s}
                for r in run_result.responses
            ],
        },
    )
