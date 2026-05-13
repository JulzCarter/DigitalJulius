"""Pipeline progress events.

The orchestrator emits a StepEvent at each stage (classify, agent call,
synth, review). CLI subscribes via an on_event callback and renders the
events live so the user sees exactly which models are working.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class StepEvent:
    kind: str          # *_start | *_done | *_skip | *_fail
    label: str         # human-readable description for the line
    agent: str = ""    # agent name involved, when applicable
    model: str = ""    # specific model in use, when applicable
    duration_s: float = 0.0  # set on *_done events
    note: str = ""     # additional detail (e.g. tier reason, error)


# A reporter takes one event and renders it. None = silent.
Reporter = Callable[[StepEvent], None]


def silent(_e: StepEvent) -> None:
    pass
