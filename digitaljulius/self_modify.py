"""Self-modification.

`/self <instruction>` lets DigitalJulius edit its OWN source code. We:
  1. Locate the repo root (the parent of the `digitaljulius` package).
  2. Have Claude Opus draft a change plan + diff against the current files.
  3. Hand the plan to Claude Code (the agent CLI) in headless YOLO mode,
     running with cwd = repo root, so it can edit files and commit.
  4. Show the user the resulting diff and ask before pushing.

Safety:
  - Refuses to run outside a git repo (so changes can be reverted).
  - Refuses if uncommitted changes already exist (clean slate first).
  - Auto-commits every accepted change so it's recoverable.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from digitaljulius.agents.registry import get_agent
from digitaljulius.budget import record_call


@dataclass
class SelfModResult:
    ok: bool
    repo: Path
    plan: str = ""
    diff: str = ""
    commit_sha: str = ""
    note: str = ""


def repo_root() -> Path:
    """Walk up from this file until we hit a git repo."""
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / ".git").exists():
            return parent
    # Fall back to the package parent (no git yet — caller will refuse).
    return here.parent.parent


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


PLAN_PROMPT = """You are about to modify the source code of DigitalJulius
itself. DigitalJulius is a Python orchestrator that routes prompts across
Claude Code, Gemini CLI, and Qwen Code.

Repo root: {repo}
Package: digitaljulius/

User instruction:
---
{instruction}
---

Produce a SHORT plan (<= 6 bullets) describing exactly which files to change
and what to add/remove. Do NOT write code yet — only the plan. Be specific
about file paths."""


EXEC_PROMPT = """You are modifying the source code of DigitalJulius itself in
the current working directory. You have full filesystem permission.

User instruction:
---
{instruction}
---

Plan that was already approved by the reviewer:
---
{plan}
---

Apply the plan now. Edit files directly. When done, print a one-line summary
of what changed. Do not run pytest; the user will run their own checks."""


def self_modify(
    instruction: str,
    cfg: dict,
    confirm_apply,
    confirm_commit,
) -> SelfModResult:
    """End-to-end self-modify flow.

    `confirm_apply(plan)` is shown the plan; returns True to proceed.
    `confirm_commit(diff)` is shown the diff; returns True to commit.
    """
    repo = repo_root()
    if not (repo / ".git").exists():
        return SelfModResult(ok=False, repo=repo, note="not a git repo — refusing to self-modify")

    # Refuse if the tree is dirty so we can always roll back cleanly.
    status = _git(["status", "--porcelain"], repo)
    if status.stdout.strip():
        return SelfModResult(
            ok=False, repo=repo,
            note="working tree is dirty; commit or stash first",
        )

    # ---- 1. Plan via Claude Opus -----------------------------------------
    approver_cfg = cfg.get("approver", {})
    planner_name = approver_cfg.get("agent", "claude")
    planner_model = approver_cfg.get("model", "opus")
    try:
        planner = get_agent(planner_name)
    except KeyError:
        return SelfModResult(ok=False, repo=repo, note=f"unknown planner agent: {planner_name}")
    if not (planner.is_installed() and planner.is_authenticated()):
        return SelfModResult(ok=False, repo=repo, note=f"{planner_name} not authenticated")

    plan_prompt = PLAN_PROMPT.replace("{repo}", str(repo)).replace("{instruction}", instruction)
    plan_resp = planner.run(plan_prompt, model=planner_model, yolo=True, cwd=repo, timeout=180)
    record_call(planner_name, planner_model)
    if not plan_resp.ok or not plan_resp.text:
        return SelfModResult(
            ok=False, repo=repo, note=f"planner failed: {plan_resp.stderr or 'no output'}"
        )
    plan = plan_resp.text

    if not confirm_apply(plan):
        return SelfModResult(ok=False, repo=repo, plan=plan, note="user declined plan")

    # ---- 2. Execute via Claude Code (file-editing agent) ------------------
    executor = get_agent("claude")
    if not (executor.is_installed() and executor.is_authenticated()):
        return SelfModResult(
            ok=False, repo=repo, plan=plan,
            note="claude executor not authenticated",
        )
    exec_prompt = EXEC_PROMPT.replace("{instruction}", instruction).replace("{plan}", plan)
    exec_resp = executor.run(exec_prompt, model="sonnet", yolo=True, cwd=repo, timeout=600)
    record_call("claude", "sonnet")
    if not exec_resp.ok:
        return SelfModResult(
            ok=False, repo=repo, plan=plan,
            note=f"executor failed: {exec_resp.stderr or 'no output'}",
        )

    # ---- 3. Show the diff and ask before committing ----------------------
    diff = _git(["diff", "--stat"], repo).stdout
    full_diff = _git(["diff"], repo).stdout
    if not diff.strip() and not full_diff.strip():
        return SelfModResult(
            ok=True, repo=repo, plan=plan,
            note="executor reported success but no files changed",
        )

    if not confirm_commit(diff + "\n\n" + full_diff[:4000]):
        # Revert.
        _git(["checkout", "--", "."], repo)
        return SelfModResult(
            ok=False, repo=repo, plan=plan, diff=diff,
            note="user declined diff — changes reverted",
        )

    # ---- 4. Commit -------------------------------------------------------
    _git(["add", "-A"], repo)
    commit_msg = f"self: {instruction[:72]}"
    commit = _git(["commit", "-m", commit_msg], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    return SelfModResult(
        ok=True, repo=repo, plan=plan, diff=diff,
        commit_sha=sha[:12],
        note=commit.stdout.strip().splitlines()[0] if commit.stdout else "committed",
    )


def reinstall() -> tuple[bool, str]:
    """Pip-reinstall the package so source edits take effect next launch."""
    import sys
    repo = repo_root()
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", str(repo), "--quiet"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()
