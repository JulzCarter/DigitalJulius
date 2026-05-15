"""Mid-progress reporter — auto-surface URLs, files, and dev-server links.

Watches the working directory for new/modified files between events and
parses agent output for URL patterns. Surfaces them to the user as they
happen so building an app produces "📁 created: ..." / "🔗 URL: ..." lines
in real time, not after the fact.

Invoked by the orchestrator at every stage transition. Cheap: just a dict
of mtimes and a regex over recent stderr/stdout.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from digitaljulius.log import get_logger

log = get_logger(__name__)


# Match http(s)://host[:port][/path]. Greedy enough to grab dev-server URLs
# from agent stderr like "Local: http://localhost:5173/" or "Listening on
# http://0.0.0.0:8080".
URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)


@dataclass
class ProgressReporter:
    """Tracks file changes + URL emissions in a working directory.

    Pass a `notify` callback that takes a single line of text — the CLI's
    `_live_reporter` is the obvious target.
    """
    cwd: Path
    notify: Callable[[str], None]
    ignore_dirs: set[str] = field(default_factory=lambda: {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", ".next", ".cache", ".pytest_cache",
    })
    max_files: int = 5000  # don't recurse forever in huge repos
    _mtimes: dict[str, float] = field(default_factory=dict)
    _seen_urls: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._snapshot()

    def _snapshot(self) -> None:
        """Record current mtimes so we can diff later."""
        try:
            self._mtimes = self._scan()
        except Exception as e:
            log.warning("progress.snapshot_failed err=%s", e)

    def _scan(self) -> dict[str, float]:
        result: dict[str, float] = {}
        count = 0
        for root, dirs, files in os.walk(self.cwd):
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs]
            for f in files:
                p = os.path.join(root, f)
                try:
                    result[p] = os.path.getmtime(p)
                except OSError:
                    continue
                count += 1
                if count >= self.max_files:
                    return result
        return result

    def diff_files(self) -> tuple[list[str], list[str]]:
        """Return (created, modified) since last snapshot, then re-snapshot."""
        try:
            current = self._scan()
        except Exception as e:
            log.warning("progress.scan_failed err=%s", e)
            return [], []
        created: list[str] = []
        modified: list[str] = []
        for path, mt in current.items():
            old = self._mtimes.get(path)
            if old is None:
                created.append(path)
            elif mt > old + 0.5:  # 0.5s slop to ignore noise
                modified.append(path)
        self._mtimes = current
        return created, modified

    def harvest(self, agent_output: str = "") -> None:
        """Emit progress lines for any new files + any URLs in agent output."""
        created, modified = self.diff_files()
        for p in created[:8]:  # cap so a `pip install` doesn't spam
            rel = os.path.relpath(p, self.cwd)
            self.notify(f"📁 created: {rel}")
        if len(created) > 8:
            self.notify(f"📁 +{len(created) - 8} more files created")
        for p in modified[:6]:
            rel = os.path.relpath(p, self.cwd)
            self.notify(f"✏️  modified: {rel}")
        if agent_output:
            for url in URL_RE.findall(agent_output):
                # Trim trailing punctuation often glued to URLs in prose.
                url = url.rstrip(".,);:'\"")
                if url in self._seen_urls:
                    continue
                self._seen_urls.add(url)
                self.notify(f"🔗 {url}")
        log.debug("progress.harvest created=%d modified=%d urls=%d",
                  len(created), len(modified), len(self._seen_urls))
