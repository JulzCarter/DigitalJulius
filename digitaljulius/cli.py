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
    interactive_login,
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


def _login_wizard(force: bool = False) -> bool:
    """Walk the user through OAuth for each unauthenticated agent.

    Spawns the agent's CLI attached to this terminal so the user can complete
    the flow in place — same UX as launching `claude` / `gemini` / `qwen` cold.
    Returns True if at least one agent ends up authenticated.
    """
    probes = probe()
    if fully_authenticated(probes) and not force:
        return True

    ui.banner()
    ui.info("Checking agent authentication…")
    ui.render_auth(probes)
    ui.console.print()

    targets = [p for p in probes if force or not p.authenticated]
    if not targets:
        return True

    if not force:
        ui.warn(
            f"{len(targets)} agent(s) need auth. We'll walk through each one — "
            "the agent's own OAuth flow runs in this terminal, then exit its "
            "TUI (e.g. /quit or Ctrl+C) to come back to DigitalJulius."
        )

    for p in targets:
        if not p.installed:
            ui.warn(f"{p.agent}: {p.note} — skipping")
            continue

        if p.authenticated and not force:
            continue

        verb = "Re-authenticate" if p.authenticated else "Log in to"
        ui.console.print()
        ui.console.print(f"[bold cyan]→ {p.agent}[/bold cyan]  "
                         f"[dim]{instructions_for(p.agent)}[/dim]")
        ui.console.print(f"  {verb} {p.agent} now?")
        ui.console.print("    [bold]1)[/bold] Yes — log in")
        ui.console.print("    [bold]2)[/bold] No — skip")
        try:
            choice = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ui.warn(f"skipped {p.agent}")
            continue
        if choice not in {"1", "y", "yes"}:
            ui.warn(f"skipped {p.agent} — run /auth later if you change your mind")
            continue

        ui.info(
            f"launching {p.agent} here — its OAuth will open a browser. "
            f"After it finishes, exit the agent's TUI (Ctrl+C or its /quit) "
            "to return to DigitalJulius."
        )
        ok = interactive_login(p.agent)
        if ok:
            ui.info(f"{p.agent} authenticated")
        else:
            ui.warn(f"{p.agent} still not authenticated — try `/auth {p.agent}` later")

    mark_first_run_complete()
    final = probe()
    ui.console.print()
    ui.render_auth(final)
    return any(pp.authenticated for pp in final)


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
    login: bool = typer.Option(
        False, "--login", help="force the OAuth walkthrough for every agent"
    ),
) -> None:
    """DigitalJulius — orchestrator across Claude / Gemini / Qwen."""
    cfg = load_config()
    save_config(cfg)  # writes defaults on first run
    cfg["general"]["yolo_default"] = yolo
    cfg["general"].setdefault("auto_learn", True)
    ensure_kb()
    work_cwd = (cwd or Path.cwd()).resolve()

    # Headless one-shots skip the interactive wizard — they're typically
    # scripted; if auth is missing the orchestrator will surface that itself.
    if not prompt:
        if not _login_wizard(force=login):
            ui.error(
                "no agents authenticated; nothing to route to. "
                "Re-run `dj --login` to retry."
            )
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
