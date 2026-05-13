"""DigitalJulius CLI entry point.

Run modes:
    digitaljulius                       interactive TUI
    digitaljulius -p "<prompt>"         one-shot headless
    dj                                  same as above (short alias)
"""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory

from digitaljulius import ui
from digitaljulius.agents.registry import get_agent
from digitaljulius.auth import (
    fully_authenticated,
    first_run_completed,
    instructions_for,
    mark_first_run_complete,
    probe,
)
from digitaljulius.budget import best_available_model, record_call
from digitaljulius.commands import command_names, dispatch
from digitaljulius.config import load_config, save_config
from digitaljulius.knowledge import auto_distill, ensure_kb
from digitaljulius.orchestrator import run_prompt
from digitaljulius.state import new_session, turn_from_runresult



def _confirm_twin(question: str) -> bool:
    ui.console.print(f"[yellow]{question}[/yellow]", end="")
    try:
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _first_run_wizard() -> None:
    ui.banner()
    ui.info("First-run setup — checking agent auth…")
    probes = probe()
    ui.render_auth(probes)
    if fully_authenticated(probes):
        ui.info("All agents authenticated. You're good to go.")
        mark_first_run_complete()
        return
    ui.warn("Some agents need auth. Follow the steps below, then re-run.")
    for p in probes:
        if not p.authenticated:
            ui.console.print(f"\n[bold]{p.agent}[/bold]: {instructions_for(p.agent)}")
    ui.console.print()
    ui.info("Once you've signed each one in, run `digitaljulius` again.")
    mark_first_run_complete()


def _render_run(prompt: str, run_result) -> None:
    cls = run_result.classification
    ui.status_line(
        agent=run_result.chosen_agent or "-",
        model=run_result.chosen_model or "-",
        tier=cls.tier.value,
        yolo=True,
    )
    if run_result.plan_verdict is not None:
        ui.render_verdict("plan-review", run_result.plan_verdict)
    if run_result.skipped_reason and not run_result.final_text:
        ui.error(run_result.skipped_reason)
        return
    ui.render_response(run_result.final_text)
    if run_result.output_verdict is not None:
        ui.render_verdict("output-review", run_result.output_verdict)
    if run_result.skipped_reason:
        ui.warn(run_result.skipped_reason)


def _run_single_pinned(prompt: str, agent: str, cfg: dict, cwd: Path) -> None:
    model = best_available_model(cfg, agent)
    if not model:
        ui.error(f"{agent} has no models left under quota")
        return
    adapter = get_agent(agent)
    ui.status_line(agent=agent, model=model, tier="PINNED", yolo=True)
    resp = adapter.run(prompt, model=model, yolo=True, cwd=cwd, timeout=300)
    record_call(agent, model)
    if resp.ok:
        ui.render_response(resp.text)
    else:
        ui.error(resp.stderr or "agent failed")


def _interactive_loop(cfg: dict, cwd: Path) -> None:
    session = new_session(cwd)
    history = InMemoryHistory()
    completer = WordCompleter(command_names(), ignore_case=True, sentence=True)
    pt = PromptSession(history=history, completer=completer)

    ctx: dict = {
        "cfg": cfg,
        "cwd": cwd,
        "session": session,
        "yolo": True,
        "quit": False,
    }

    ui.banner()
    ui.info(f"cwd: {cwd}")
    ui.info("type /help for commands, /quit to exit. Ctrl-D also exits.")

    while not ctx["quit"]:
        try:
            line = pt.prompt("dj » ").strip()
        except (EOFError, KeyboardInterrupt):
            ui.console.print()
            break
        if not line:
            continue

        if dispatch(line, ctx):
            forced = ctx.pop("force_consensus", False)
            queued = ctx.pop("pending_prompt", None)
            if forced and queued:
                _run_forced_consensus(queued, ctx)
            continue

        pinned = ctx.get("pinned_agent")
        if pinned:
            _run_single_pinned(line, pinned, cfg, cwd)
            continue

        try:
            run_result = run_prompt(line, cfg, cwd=cwd, confirm=_confirm_twin)
        except KeyboardInterrupt:
            ui.warn("interrupted")
            continue
        _render_run(line, run_result)
        session.append(turn_from_runresult(line, run_result))

        # Self-improvement: distill a durable lesson from this turn.
        if cfg.get("general", {}).get("auto_learn", True):
            try:
                entry = auto_distill(
                    prompt=line,
                    tier=run_result.classification.tier.value,
                    chosen_agent=run_result.chosen_agent or "",
                    chosen_model=run_result.chosen_model or "",
                    output=run_result.final_text or "",
                    approved=(
                        run_result.output_verdict.approved
                        if run_result.output_verdict else None
                    ),
                    cfg=cfg,
                    cwd=cwd,
                )
                if entry:
                    ui.info(f"learned ({entry.kind}): {entry.text}")
            except Exception as e:
                # Never let learning kill the loop.
                ui.warn(f"distill skipped: {e}")

    ui.info(f"session log: {session.log_path()}")


def _run_forced_consensus(prompt: str, ctx: dict) -> None:
    """Run /consensus by upgrading the prompt to CRITICAL and going through the
    orchestrator so the approver review still happens."""
    cfg = ctx["cfg"]
    cwd = ctx["cwd"]
    # Hack: temporarily inject the word 'critical' so classifier picks CRITICAL,
    # then run normally. We keep the original prompt for display.
    forced_prompt = f"[critical] {prompt}"
    run_result = run_prompt(forced_prompt, cfg, cwd=cwd, confirm=_confirm_twin)
    _render_run(prompt, run_result)
    if ctx.get("session"):
        ctx["session"].append(turn_from_runresult(prompt, run_result))


def _run(
    prompt: str | None = typer.Option(
        None, "-p", "--prompt", help="run a single prompt headless and exit"
    ),
    yolo: bool = typer.Option(True, "--yolo/--safe", help="skip permission prompts"),
    cwd: Path | None = typer.Option(None, "--cwd", help="working directory"),
) -> None:
    """DigitalJulius — orchestrator across Claude / Gemini / Qwen."""
    cfg = load_config()
    save_config(cfg)  # writes defaults on first run
    cfg["general"]["yolo_default"] = yolo
    cfg["general"].setdefault("auto_learn", True)
    ensure_kb()
    work_cwd = (cwd or Path.cwd()).resolve()

    if not first_run_completed():
        _first_run_wizard()
        # If they aren't authenticated yet, bail so they can fix it.
        if not fully_authenticated(probe()):
            return

    if prompt:
        run_result = run_prompt(prompt, cfg, cwd=work_cwd, confirm=_confirm_twin)
        _render_run(prompt, run_result)
        return

    _interactive_loop(cfg, work_cwd)


def main() -> None:
    typer.run(_run)


if __name__ == "__main__":
    main()
