"""Centralised logging — rotating file + live colored console.

This is the *real* logger (Python's logging module). It is independent of
the per-turn JSONL session log in state.py, which records final outcomes
only. This logger captures the work-in-progress: every classifier call,
adapter dispatch, fallback decision, role escalation, and quota event.

Log file: ~/.digitaljulius/logs/digitaljulius.log (rotated at 5 MB, 5 backups).

The CLI also surfaces these events live to the terminal via the `events.py`
StepEvent stream — `setup()` registers a handler that mirrors INFO+ to the
console with rich formatting, so the user sees what's happening in real time.

Call `setup()` exactly once at process start. `get_logger(name)` is a thin
wrapper around `logging.getLogger` so module-level imports stay terse.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

_DONE = False
_LOG_PATH: Path | None = None


def _log_dir() -> Path:
    # We can't import digitaljulius.config here at module-import time — that
    # would create a cycle (config.py is sometimes imported very early). Re-
    # derive the path from $HOME directly.
    return Path(os.path.expanduser("~")) / ".digitaljulius" / "logs"


def setup(level: str | int = "INFO", *, console: bool = True) -> Path:
    """Idempotently configure root logger. Returns the log file path."""
    global _DONE, _LOG_PATH
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "digitaljulius.log"
    _LOG_PATH = log_path

    if _DONE:
        return log_path

    root = logging.getLogger("digitaljulius")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(level)
    root.propagate = False

    # Rotating file: 5MB x 5 = 25MB ceiling. UTF-8 so emoji don't crash on Windows.
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)  # file gets everything
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler(stream=sys.stderr)
        ch.setLevel(logging.WARNING)  # console stays quiet; live UI handles INFO
        ch.setFormatter(logging.Formatter(
            "%(levelname)s %(name)s: %(message)s"
        ))
        root.addHandler(ch)

    _DONE = True
    root.info("=" * 60)
    root.info("digitaljulius logger initialised — level=%s file=%s",
              logging.getLevelName(level), log_path)
    return log_path


def get_logger(name: str) -> logging.Logger:
    """`get_logger("digitaljulius.agents.openai")` style. Auto-runs setup."""
    if not _DONE:
        try:
            setup()
        except Exception:
            # Never let logging setup kill the import — fall through to the
            # bare logger which still works (just no file).
            pass
    return logging.getLogger(name)


def log_path() -> Path | None:
    return _LOG_PATH


def set_console_verbose(verbose: bool) -> None:
    """Bump console handler down to INFO when --verbose is passed."""
    root = logging.getLogger("digitaljulius")
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.handlers.RotatingFileHandler
        ):
            h.setLevel(logging.INFO if verbose else logging.WARNING)
