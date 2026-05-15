"""DigitalJulius CLI entry point.

Run modes:
    digitaljulius                       interactive TUI
    digitaljulius -p "<prompt>"         one-shot headless
    dj                                  same as above (short alias)
"""
from __future__ import annotations

import getpass
import sys
import time as _time
from pathlib import Path

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory
from rich.status import Status

from digitaljulius import secrets, ui
from digitaljulius.agents.registry import get_agent
from digitaljulius.auth import (
    fully_authenticated,
    first_run_completed,
    instructions_for,
    interactive_login,
    mark_first_run_complete,
    probe,
    reset_credentials,
)
from digitaljulius.budget import best_available_model, record_call
from digitaljulius.commands import command_names, dispatch
from digitaljulius.config import load_config, save_config
from digitaljulius.events import StepEvent
from digitaljulius.history import build_history_context
from digitaljulius.knowledge import auto_distill, ensure_kb
from digitaljulius.log import get_logger, log_path, set_console_verbose
from digitaljulius.orchestrator import _single_agent_run, run_prompt
from digitaljulius.planning import Plan
from digitaljulius.project_ctx import collect_project_context
from digitaljulius.state import new_session, turn_from_runresult

log = get_logger(__name__)


def _is_dangerous_cwd(cwd: Path) -> tuple[bool, str]:
    """Return (is_dangerous, why). Home dir + drive roots are dangerous in YOLO
    mode because workers may scan and pick a random project to act on."""
    cwd = cwd.resolve()
    home = Path.home().resolve()
    if cwd == home:
        return True, "user home directory"
    # Windows drive root (C:\, D:\) or POSIX root.
    if cwd.parent == cwd:
        return True, "filesystem root"
    if str(cwd).rstrip("\\/") in {"C:", "D:", "E:", "F:"}:
        return True, "drive root"
    return False, ""


def _warn_dangerous_cwd(cwd: Path) -> None:
    """If `dj` is launched from home/root, warn the user — YOLO mode there
    means any worker can read/edit unrelated projects in your home."""
    bad, why = _is_dangerous_cwd(cwd)
    if not bad:
        return
    ui.console.print()
    ui.console.print(
        f"[bold yellow]⚠ cwd = {cwd}  ({why})[/bold yellow]"
    )
    ui.console.print(
        "[dim]YOLO workers will scan this directory for context. From "
        "your home dir they can stumble into unrelated projects (e.g. a "
        "Gemini run picked up `BrunchBuddy` from C:\\Users\\juliu and "
        "started building it instead of your prompt).[/dim]"
    )
    ui.console.print(
        "[dim]Strongly recommend cd'ing into the project you want to work "
        "on, then re-running `dj`.[/dim]"
    )
    ui.console.print()


def _build_context(cfg: dict, cwd: Path, session) -> tuple[str, str]:
    """Return (project_context, history_context) blocks, possibly empty.
    Kept separate from the user's prompt so the classifier doesn't get
    overwhelmed by 4 KB of project memory before it picks a tier."""
    mem_cfg = cfg.get("memory", {})
    proj = collect_project_context(cwd, max_chars=mem_cfg.get("project_chars", 4000))
    hist = ""
    if session is not None:
        hist = build_history_context(
            session.turns,
            max_turns=mem_cfg.get("history_turns", 4),
            max_chars=mem_cfg.get("history_chars", 3500),
        )
    return proj, hist



# Phase emoji mapping. Determines which icon precedes the spinner label so
# the user can see at-a-glance whether we're thinking, planning, executing,
# auditing, or escalating between bosses.
_PHASE_EMOJI = {
    "classify":     "🧠",  # thinking — picking tier
    "plan_draft":   "📋",  # planning — drafting plan
    "plan_review":  "📋",
    "agent":        "⚡",  # executing — worker doing the actual work
    "review":       "🔍",  # reviewing — approver checking output
    "synth":        "🧩",  # synthesising — merging consensus
    "twin":         "👯",
    "route":        "🧭",
    "progress":     "📁",
    "escalate":     "⚡",  # bold yellow handoff
}

# Module-level spinner state. The orchestrator runs synchronously on the
# main thread, so events arrive serially — no thread-safety needed.
_live_status: Status | None = None
_live_t0: float = 0.0
_live_phase: str = ""


def _phase_kind(event_kind: str) -> str:
    """Map 'agent_start' → 'agent', 'plan_draft_done' → 'plan_draft', etc."""
    for suffix in ("_start", "_done", "_fail", "_skip"):
        if event_kind.endswith(suffix):
            return event_kind[: -len(suffix)]
    return event_kind


def _begin_phase(phase: str, agent: str, label: str, model: str = "") -> None:
    """Start (or replace) the live spinner with a new phase label."""
    global _live_status, _live_t0, _live_phase
    _end_phase()
    emoji = _PHASE_EMOJI.get(phase, "•")
    parts = [emoji, f"[bold]{phase}[/bold]"]
    if agent:
        parts.append(f"· [magenta]{agent}[/magenta]")
    if model:
        parts.append(f"[cyan]{model}[/cyan]")
    parts.append(f"— {label}")
    desc = " ".join(parts)
    _live_t0 = _time.monotonic()
    _live_phase = phase
    try:
        _live_status = ui.console.status(desc, spinner="dots")
        _live_status.start()
    except Exception:
        # Fallback: print the line statically if Rich Status can't init
        # (e.g. non-tty stdout, IDE buffered output).
        _live_status = None
        ui.console.print(f"● {desc}")


def _end_phase(final_msg: str = "") -> None:
    """Stop the spinner and optionally print a one-line summary in its place."""
    global _live_status, _live_phase
    if _live_status is not None:
        try:
            _live_status.stop()
        except Exception:
            pass
        _live_status = None
    _live_phase = ""
    if final_msg:
        ui.console.print(final_msg)


def _live_reporter(event: StepEvent) -> None:
    """Live phased status display.

    Maintains a single Rich `Status` spinner that mutates per phase
    (🧠 thinking → 📋 planning → ⚡ executing → 🔍 reviewing → 🧩 synthesising)
    so the user sees motion DURING long agent calls, not just at the end.
    Every event is also mirrored to the rotating log file.
    """
    log.info("event kind=%s agent=%s model=%s dur=%.1fs label=%s note=%s",
             event.kind, event.agent or "-", event.model or "-",
             event.duration_s or 0.0, event.label, (event.note or "")[:200])

    phase = _phase_kind(event.kind)

    # Special: escalate is a one-shot handoff banner, not a phase boundary.
    if event.kind == "escalate_done":
        _end_phase()
        ui.console.print(f"[bold yellow]⚡ {event.label}[/bold yellow]")
        return

    # Progress lines (📁 created, ✏️ modified, 🔗 URL) — print above the
    # current spinner without disturbing it.
    if event.kind == "progress_done":
        if _live_status is not None:
            ui.console.log(event.label)
        else:
            ui.console.print(f"  [dim]{event.label}[/dim]")
        return

    if event.kind.endswith("_start"):
        agent = event.agent or ""
        label = event.label
        # Friendlier labels per phase
        if phase == "classify":
            label = "thinking — picking tier and tags"
        elif phase == "plan_draft":
            label = "planning — drafting structured steps"
        elif phase == "agent":
            label = f"executing — {event.label}"
        elif phase == "review":
            label = "reviewing — approver checking output"
        elif phase == "synth":
            label = "synthesising — merging agent voices"
        _begin_phase(phase, agent, label, event.model or "")
        return

    if event.kind.endswith("_done"):
        elapsed = event.duration_s or (_time.monotonic() - _live_t0)
        head = "[green]✔[/green]"
        body = event.label
        if event.agent:
            body = f"[magenta]{event.agent}[/magenta] — {event.label}"
        line = f"  {head} {body} [dim]({elapsed:.1f}s)[/dim]"
        if event.note:
            line += f"\n    [dim]{event.note}[/dim]"
        _end_phase(line)
        return

    if event.kind.endswith("_fail"):
        elapsed = event.duration_s or (_time.monotonic() - _live_t0)
        head = "[red]✖[/red]"
        line = f"  {head} {event.label} [dim]({elapsed:.1f}s)[/dim]"
        if event.note:
            line += f"\n    [dim red]{event.note[:200]}[/dim red]"
        _end_phase(line)
        return

    if event.kind.endswith("_skip"):
        _end_phase(f"  [yellow]↷[/yellow] {event.label} [dim]({event.note})[/dim]")
        return

    # Unknown event kind — print it without disturbing the spinner.
    if _live_status is not None:
        ui.console.log(event.label)
    else:
        ui.console.print(f"  {event.label}")


def _confirm_twin(question: str) -> bool:
    ui.console.print(f"[yellow]{question}[/yellow]", end="")
    try:
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _confirm_planning_choice(options: list[tuple[str, str]]) -> tuple[str, str] | None:
    """USER-LOCKED RULE: never silently downgrade planning models.

    Called by roles._planning_role_call when every top-tier planner (claude:opus,
    openai:gpt-5, codex:gpt-5-codex) is exhausted. The user must explicitly
    pick a downgrade or abort.
    """
    if not options:
        ui.error("no fallback planner models available — try /openai set-key or wait for quota reset")
        return None
    ui.console.print()
    ui.console.print("[bold red]⚠ all top-tier planners exhausted[/bold red]")
    ui.console.print("[dim]Per your standing rule, planning never silently "
                     "drops to a lower-tier model. Pick one explicitly:[/dim]")
    for i, (agent, model) in enumerate(options, start=1):
        ui.console.print(f"  [bold]{i})[/bold] [magenta]{agent}[/magenta] · [cyan]{model}[/cyan]")
    ui.console.print(f"  [bold]0)[/bold] abort this turn")
    ui.console.print("  [dim]choose 1-N or 0:[/dim] ", end="")
    try:
        raw = input().strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw or raw == "0":
        return None
    try:
        idx = int(raw)
        if 1 <= idx <= len(options):
            return options[idx - 1]
    except ValueError:
        pass
    ui.warn("invalid choice — aborting")
    return None


def _review_drafted_plan(plan: Plan) -> Plan | None:
    """Show a drafted plan; user picks: + expand / approve / edit / abort.

    Default view is collapsed: just the summary + step count. The user types
    `+` to expand (full steps/risks/artifacts), `-` to collapse, `y` to
    approve, `n` to abort, or `e` to edit one step inline.
    """
    if plan.is_empty():
        ui.warn("planner returned an empty plan — aborting")
        return None

    expanded = False
    while True:
        ui.console.print()
        ui.console.print(f"[bold cyan]📋 plan[/bold cyan]  {plan.summary}")
        ui.console.print(f"[dim]{len(plan.steps)} step(s), "
                         f"{len(plan.risks)} risk(s), "
                         f"{len(plan.artifacts)} artifact(s)[/dim]")
        if expanded:
            if plan.steps:
                ui.console.print("[bold]steps:[/bold]")
                for i, s in enumerate(plan.steps, 1):
                    ui.console.print(f"  [bold]{i}.[/bold] {s}")
            if plan.artifacts:
                ui.console.print("[bold]artifacts you'll see:[/bold]")
                for a in plan.artifacts:
                    ui.console.print(f"  - [cyan]{a}[/cyan]")
            if plan.risks:
                ui.console.print("[bold]risks:[/bold]")
                for r in plan.risks:
                    ui.console.print(f"  - [yellow]{r}[/yellow]")
        else:
            ui.console.print("[dim]press [bold]+[/bold] to expand · "
                             "[bold]y[/bold] approve · [bold]e[/bold] edit step · "
                             "[bold]n[/bold] abort[/dim]")
        ui.console.print("  [dim]choice:[/dim] ", end="")
        try:
            raw = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw == "+":
            expanded = True
            continue
        if raw == "-":
            expanded = False
            continue
        if raw in {"y", "yes", "ok", ""}:
            plan.approved = True
            return plan
        if raw in {"n", "no", "abort", "q"}:
            return None
        if raw in {"e", "edit"}:
            ui.console.print("  step number to edit (or 0 to cancel): ", end="")
            try:
                idx = int(input().strip())
            except (ValueError, EOFError):
                continue
            if not (1 <= idx <= len(plan.steps)):
                continue
            ui.console.print(f"  current: {plan.steps[idx - 1]}")
            ui.console.print("  new text (blank = keep): ", end="")
            try:
                new_text = input().strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if new_text:
                plan.steps[idx - 1] = new_text
                plan.edited_by_user = True
            continue
        ui.warn("expected + / - / y / n / e")


def _login_wizard(force: bool = False) -> bool:
    """Walk the user through OAuth for each unauthenticated agent.

    Spawns the agent's CLI attached to this terminal so the user can complete
    the flow in place — same UX as launching `claude` / `gemini` / `gh` cold.
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
            if p.agent == "codex":
                ui.console.print(
                    "  [yellow]Codex CLI not installed yet.[/yellow] "
                    "Run [cyan]C:\\Users\\juliu\\Downloads\\Codex Installer.exe[/cyan] "
                    "then [cyan]codex login[/cyan] to enable deep-coding mode."
                )
            continue

        ui.console.print()
        ui.console.print(f"[bold cyan]→ {p.agent}[/bold cyan]  "
                         f"[dim]{instructions_for(p.agent)}[/dim]")

        # OpenAI doesn't have an OAuth flow — it uses an API key. Prompt
        # for it here so Julius's $250 API credits are immediately usable.
        if p.agent == "openai":
            if p.authenticated and not force:
                continue
            ui.console.print(
                "  Paste your OpenAI API key (starts with [cyan]sk-[/cyan]). "
                "Stored at ~/.digitaljulius/secrets.json (chmod 600)."
            )
            ui.console.print("  Get one at: [cyan]https://platform.openai.com/api-keys[/cyan]")
            ui.console.print("  Press Enter on a blank line to skip.")
            try:
                key = getpass.getpass("  key (hidden): ").strip()
            except (EOFError, KeyboardInterrupt):
                ui.warn(f"  skipped {p.agent}")
                continue
            if not key:
                ui.warn(f"  skipped {p.agent} — set later with `/openai set-key`")
                continue
            if not key.startswith("sk-"):
                ui.warn("  key doesn't start with `sk-` — saving anyway, double-check it")
            secrets.set_("OPENAI_API_KEY", key)
            ui.info(f"  {p.agent} key stored — try `/openai test` to verify")
            continue

        if p.authenticated:
            # Already logged in. Only offer reset+re-auth in force mode, and
            # make the consequence explicit so the user picks deliberately.
            if not force:
                continue
            ui.console.print(f"  {p.agent} is already authenticated.")
            ui.console.print(
                "    [bold]1)[/bold] Keep current login [dim](recommended)[/dim]"
            )
            ui.console.print(
                "    [bold]2)[/bold] Reset credentials and re-authenticate "
                "[red](will log out everywhere)[/red]"
            )
            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                continue
            if choice not in {"2", "reset"}:
                continue
            wiped = reset_credentials(p.agent)
            if wiped is None:
                ui.warn(f"could not delete {p.agent}'s credentials — skipping")
                continue
            ui.info(f"deleted {wiped} — launching {p.agent} for a fresh OAuth flow")
        else:
            ui.console.print(f"  Log in to {p.agent} now?")
            ui.console.print("    [bold]1)[/bold] Yes — open OAuth (browser)")
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
                f"launching {p.agent} — its OAuth will open a browser. "
                "After it finishes, exit the agent's TUI (Ctrl+C or its /quit) "
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
    """Run a prompt with all routing turned off (user pinned this agent).
    Still gets same-agent model fallback so credits-out on opus rolls down
    to sonnet/haiku automatically."""
    model = best_available_model(cfg, agent)
    if not model:
        ui.error(f"{agent} has no models left under quota — try `/switch <other>`")
        return
    ui.status_line(agent=agent, model=model, tier="PINNED", yolo=True)
    resp = _single_agent_run(prompt, agent, cfg, cwd, _live_reporter)
    if resp.ok:
        ui.render_response(resp.text)
    elif resp.stderr == "QUOTA_EXCEEDED":
        ui.error(
            f"{agent} exhausted for today. "
            "Use `/switch <agent>` to pin a different one, "
            "or `/switch off` to let DigitalJulius auto-route."
        )
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
            plan_run = ctx.pop("force_plan_run", False)
            queued = ctx.pop("pending_prompt", None)
            if forced and queued:
                _run_forced_consensus(queued, ctx)
            elif plan_run and queued:
                _run_plan_first(queued, ctx)
            continue

        proj_ctx, hist_ctx = _build_context(cfg, cwd, session)
        pinned = ctx.get("pinned_agent")
        if pinned:
            pinned_prompt = line
            if proj_ctx or hist_ctx:
                pinned_prompt = "\n\n".join(p for p in (proj_ctx, hist_ctx, f"---\nUser prompt:\n{line}") if p)
            _run_single_pinned(pinned_prompt, pinned, cfg, cwd)
            continue

        force_plan_first = ctx.pop("force_plan_first", False)
        try:
            run_result = run_prompt(
                line, cfg, cwd=cwd,
                confirm=_confirm_twin, on_event=_live_reporter,
                project_context=proj_ctx, history_context=hist_ctx,
                confirm_planning=_confirm_planning_choice,
                review_drafted_plan=_review_drafted_plan,
                force_plan_first=force_plan_first,
            )
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
    lp = log_path()
    if lp:
        ui.info(f"runtime log: {lp}")


def _run_plan_first(prompt: str, ctx: dict) -> None:
    """Execute a prompt in forced plan-first mode (set by /plan slash)."""
    cfg = ctx["cfg"]
    cwd = ctx["cwd"]
    proj_ctx, hist_ctx = _build_context(cfg, cwd, ctx.get("session"))
    try:
        run_result = run_prompt(
            prompt, cfg, cwd=cwd,
            confirm=_confirm_twin, on_event=_live_reporter,
            project_context=proj_ctx, history_context=hist_ctx,
            confirm_planning=_confirm_planning_choice,
            review_drafted_plan=_review_drafted_plan,
            force_plan_first=True,
        )
    except KeyboardInterrupt:
        ui.warn("interrupted")
        return
    _render_run(prompt, run_result)
    if ctx.get("session"):
        ctx["session"].append(turn_from_runresult(prompt, run_result))


def _run_forced_consensus(prompt: str, ctx: dict) -> None:
    """Run /consensus by upgrading the prompt to CRITICAL and going through the
    orchestrator so the approver review still happens."""
    cfg = ctx["cfg"]
    cwd = ctx["cwd"]
    # Hack: temporarily inject the word 'critical' so classifier picks CRITICAL,
    # then run normally. We keep the original prompt for display.
    forced_prompt = f"[critical] {prompt}"
    run_result = run_prompt(
        forced_prompt, cfg, cwd=cwd,
        confirm=_confirm_twin, on_event=_live_reporter,
        confirm_planning=_confirm_planning_choice,
        review_drafted_plan=_review_drafted_plan,
    )
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
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="stream INFO logs to console (else file only)"
    ),
) -> None:
    """DigitalJulius — orchestrator: Claude (boss) → OpenAI (secondary) → Codex/Gemini/GitHub."""
    set_console_verbose(verbose)
    log.info("dj.start prompt=%s verbose=%s cwd=%s",
             "<one-shot>" if prompt else "<interactive>", verbose, cwd or Path.cwd())
    cfg = load_config()
    save_config(cfg)  # writes defaults on first run
    cfg["general"]["yolo_default"] = yolo
    cfg["general"].setdefault("auto_learn", True)
    ensure_kb()
    work_cwd = (cwd or Path.cwd()).resolve()
    _warn_dangerous_cwd(work_cwd)

    # Headless one-shots skip the interactive wizard — they're typically
    # scripted; if auth is missing the orchestrator will surface that itself.
    if not prompt:
        # Always nudge for the OpenAI key on session start if it's missing,
        # so Julius's $250 API credits + Pro 20x plan are usable immediately.
        if not secrets.has("OPENAI_API_KEY"):
            ui.warn(
                "OpenAI API key not configured — secondary boss is offline. "
                "Use `/openai set-key sk-…` once we're in the REPL, or run "
                "`dj --login` to walk the wizard now."
            )
        if not _login_wizard(force=login):
            ui.error(
                "no agents authenticated; nothing to route to. "
                "Re-run `dj --login` to retry."
            )
            return

    if prompt:
        run_result = run_prompt(
            prompt, cfg, cwd=work_cwd,
            confirm=_confirm_twin, on_event=_live_reporter,
            confirm_planning=_confirm_planning_choice,
            review_drafted_plan=_review_drafted_plan,
        )
        _render_run(prompt, run_result)
        return

    _interactive_loop(cfg, work_cwd)


def main() -> None:
    typer.run(_run)


if __name__ == "__main__":
    main()
