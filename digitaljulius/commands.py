"""Slash-command registry."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from digitaljulius import ui
from digitaljulius.agents.registry import get_agent
from digitaljulius.auth import (
    instructions_for,
    interactive_login,
    probe,
    reset_credentials,
)
from digitaljulius.budget import best_available_model
from digitaljulius.complexity import Tier, classify
from digitaljulius.config import CONFIG_PATH, save_config
from digitaljulius.knowledge import KB_FILES, all_entries, forget, learn
from digitaljulius.self_modify import SelfModResult, reinstall, self_modify


@dataclass
class SlashCmd:
    name: str
    help: str
    handler: Callable[..., None]


def _cmd_help(ctx) -> None:
    ui.console.print("[bold cyan]Slash commands[/bold cyan]")
    for cmd in REGISTRY.values():
        ui.console.print(f"  [bold]/{cmd.name:<10}[/bold] {cmd.help}")


def _cmd_agents(ctx) -> None:
    ui.render_agents(ctx["cfg"])


def _cmd_budget(ctx) -> None:
    ui.render_budget(ctx["cfg"])


def _cmd_auth(ctx, *args) -> None:
    """/auth [agent|all]  — show status; walk through missing auth.

      /auth                  show status, prompt to log in any missing agents
      /auth <agent>          reset & re-auth one agent (browser OAuth)
      /auth all              reset & re-auth every authenticated agent
    """
    target = args[0].lower() if args else ""
    probes = probe()
    ui.render_auth(probes)

    if not target:
        missing = [p for p in probes if not p.authenticated]
        if not missing:
            ui.info("all installed agents are authenticated. "
                    "Use `/auth <agent>` to reset and re-auth one.")
            return
        for p in missing:
            if not p.installed:
                ui.warn(f"{p.agent}: not installed — skipping")
                continue
            ui.console.print()
            ui.console.print(f"[bold cyan]→ {p.agent}[/bold cyan]  "
                             f"[dim]{instructions_for(p.agent)}[/dim]")
            ui.console.print(f"  Log in to {p.agent} now?")
            ui.console.print("    [bold]1)[/bold] Yes — open OAuth (browser)")
            ui.console.print("    [bold]2)[/bold] No — skip")
            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                continue
            if choice not in {"1", "y", "yes"}:
                continue
            ok = interactive_login(p.agent)
            ui.info(f"{p.agent}: {'authenticated' if ok else 'still not authenticated'}")
        return

    # Explicit per-agent reset path. This DELETES the existing creds so the
    # next launch triggers a fresh browser OAuth flow.
    targets = list(probes) if target == "all" else [p for p in probes if p.agent == target]
    if not targets:
        ui.error(f"unknown agent: {target}")
        return
    for p in targets:
        if not p.installed:
            ui.error(f"{p.agent}: not installed")
            continue
        ui.console.print()
        ui.console.print(f"[bold cyan]→ {p.agent}[/bold cyan]")
        if p.authenticated:
            ui.console.print(
                f"  Reset {p.agent}'s credentials and re-authenticate? "
                f"[red](will log out everywhere)[/red]"
            )
            ui.console.print("    [bold]1)[/bold] Yes — reset & open OAuth")
            ui.console.print("    [bold]2)[/bold] No — keep current login")
            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                continue
            if choice not in {"1", "y", "yes", "reset"}:
                continue
            wiped = reset_credentials(p.agent)
            if wiped is None:
                ui.warn(f"could not delete {p.agent}'s credentials")
                continue
            ui.info(f"deleted {wiped} — launching for fresh OAuth")
        else:
            ui.info(f"launching `{p.agent}` for OAuth")
        ok = interactive_login(p.agent)
        ui.info(f"{p.agent}: {'authenticated' if ok else 'still not authenticated'}")


def _cmd_clear(ctx) -> None:
    ui.console.clear()
    ui.banner()


def _cmd_quit(ctx) -> None:
    ctx["quit"] = True


def _cmd_route(ctx, *args) -> None:
    """/route <prompt> — show what tier + agent we'd pick, without running."""
    text = " ".join(args).strip()
    if not text:
        ui.warn("usage: /route <prompt>")
        return
    cls = classify(text, ctx["cfg"], cwd=ctx["cwd"])
    ui.console.print(
        f"tier=[bold]{cls.tier.value}[/bold]  "
        f"tags={cls.suggested_tags}  reason=[dim]{cls.reason}[/dim]"
    )
    routing = ctx["cfg"]["routing"]
    preferred = routing.get((cls.suggested_tags or ["default"])[0], routing["default"])
    ui.console.print(f"preferred order: {' → '.join(preferred)}")
    for agent in preferred:
        model = best_available_model(ctx["cfg"], agent)
        ui.console.print(f"  {agent}: {model or '[red]exhausted[/red]'}")


def _cmd_best(ctx, *args) -> None:
    """/best — show the top available model for each agent right now."""
    for agent in ctx["cfg"]["agents"]:
        model = best_available_model(ctx["cfg"], agent)
        ui.console.print(f"  {agent}: [cyan]{model or 'exhausted'}[/cyan]")


def _cmd_model(ctx, *args) -> None:
    """/model <agent> <model> — pin an agent's top_model (persists to config)."""
    if len(args) < 2:
        ui.warn("usage: /model <agent> <model>")
        return
    agent, model = args[0], args[1]
    if agent not in ctx["cfg"]["agents"]:
        ui.error(f"unknown agent: {agent}")
        return
    ctx["cfg"]["agents"][agent]["top_model"] = model
    chain = ctx["cfg"]["agents"][agent]["fallback_chain"]
    if model not in chain:
        chain.insert(0, model)
    save_config(ctx["cfg"])
    ui.info(f"{agent} top_model set to {model} (saved to {CONFIG_PATH})")


def _cmd_consensus(ctx, *args) -> None:
    """/consensus <prompt> — force a 3-agent consensus run regardless of tier."""
    text = " ".join(args).strip()
    if not text:
        ui.warn("usage: /consensus <prompt>")
        return
    # Signal to the main loop to run with forced consensus.
    ctx["force_consensus"] = True
    ctx["pending_prompt"] = text


def _cmd_spawn(ctx, *args) -> None:
    """/spawn <agent> — drop into a single-agent passthrough mode."""
    if not args:
        ui.warn("usage: /spawn <agent>")
        return
    agent = args[0]
    if agent not in ctx["cfg"]["agents"]:
        ui.error(f"unknown agent: {agent}")
        return
    try:
        adapter = get_agent(agent)
    except KeyError:
        ui.error(f"unknown agent: {agent}")
        return
    if not adapter.is_installed():
        ui.error(f"{agent} CLI not installed")
        return
    ctx["pinned_agent"] = agent
    ui.info(f"pinned to {agent} — next prompts go straight to it. /spawn off to unpin")


def _cmd_log(ctx) -> None:
    session = ctx.get("session")
    if session is None:
        ui.warn("no session active")
        return
    ui.render_log(session.turns)


def _cmd_init(ctx) -> None:
    """/init — drop a starter project context into ./.digitaljulius/PROJECT.md"""
    target_dir = ctx["cwd"] / ".digitaljulius"
    target_dir.mkdir(exist_ok=True)
    project_md = target_dir / "PROJECT.md"
    if project_md.exists():
        ui.warn(f"{project_md} already exists — leaving it alone")
        return
    project_md.write_text(
        "# Project context\n\n"
        "Edit this file to give DigitalJulius persistent context for this\n"
        "working directory (architecture, conventions, gotchas, etc).\n",
        encoding="utf-8",
    )
    ui.info(f"created {project_md}")


def _cmd_learn(ctx, *args) -> None:
    """/learn [kind] <text> — save a lesson to the knowledge center.
    kind ∈ {learned, preferences, routing, failures} (default learned)."""
    if not args:
        ui.warn("usage: /learn [kind] <text>")
        return
    kind = "learned"
    text_parts = list(args)
    if text_parts[0].lower() in KB_FILES:
        kind = text_parts[0].lower()
        text_parts = text_parts[1:]
    text = " ".join(text_parts).strip()
    if not text:
        ui.warn("nothing to learn — empty text")
        return
    entry = learn(text, kind=kind)
    ui.info(f"saved to {kind}: {entry.text}")


def _cmd_knowledge(ctx, *args) -> None:
    """/knowledge — show accumulated lessons."""
    entries = all_entries()
    any_shown = False
    for kind, bullets in entries.items():
        if not bullets:
            continue
        any_shown = True
        ui.console.print(f"[bold cyan]{kind}[/bold cyan] ({len(bullets)})")
        for b in bullets[-10:]:
            ui.console.print(f"  {b}")
    if not any_shown:
        ui.console.print("[dim]knowledge center is empty — use /learn to add[/dim]")


def _cmd_forget(ctx, *args) -> None:
    """/forget <needle> — remove every entry containing <needle>."""
    needle = " ".join(args).strip()
    if not needle:
        ui.warn("usage: /forget <text-to-match>")
        return
    n = forget(needle)
    ui.info(f"forgot {n} entr{'y' if n == 1 else 'ies'} matching {needle!r}")


def _confirm(prompt: str) -> bool:
    ui.console.print(f"[yellow]{prompt}[/yellow] ", end="")
    try:
        return input().strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def _cmd_self(ctx, *args) -> None:
    """/self <instruction> — let DigitalJulius edit its own source.
    Plans with Claude Opus, executes with Claude Code, commits, reinstalls."""
    instruction = " ".join(args).strip()
    if not instruction:
        ui.warn("usage: /self <what to change>")
        return

    ui.info("planning self-modification with Claude Opus…")

    def confirm_apply(plan: str) -> bool:
        ui.console.print("[bold]Planned changes:[/bold]")
        ui.console.print(plan)
        return _confirm("Proceed to apply this plan? (y/N):")

    def confirm_commit(diff: str) -> bool:
        ui.console.print("[bold]Resulting diff:[/bold]")
        ui.console.print(diff)
        return _confirm("Commit these changes? (y/N):")

    result: SelfModResult = self_modify(
        instruction, ctx["cfg"], confirm_apply, confirm_commit
    )
    if not result.ok:
        ui.error(f"self-modify aborted: {result.note}")
        return
    ui.info(f"committed {result.commit_sha} — {result.note}")
    if _confirm("Reinstall package so changes take effect next launch? (y/N):"):
        ok, msg = reinstall()
        if ok:
            ui.info("reinstalled — restart `dj` to pick up changes")
        else:
            ui.error(f"pip reinstall failed: {msg[:400]}")


def _cmd_yolo(ctx, *args) -> None:
    """/yolo on|off — toggle YOLO (dangerously-skip-permissions) mode."""
    if args and args[0].lower() in {"off", "false", "0"}:
        ctx["yolo"] = False
        ui.info("yolo OFF")
    else:
        ctx["yolo"] = True
        ui.info("yolo ON")


REGISTRY: dict[str, SlashCmd] = {
    "help":      SlashCmd("help",      "show this list",                                _cmd_help),
    "agents":    SlashCmd("agents",    "list configured agents",                        _cmd_agents),
    "budget":    SlashCmd("budget",    "show daily-quota usage",                        _cmd_budget),
    "auth":      SlashCmd("auth",      "probe agent auth status",                       _cmd_auth),
    "route":     SlashCmd("route",     "classify a prompt without running it",          _cmd_route),
    "best":      SlashCmd("best",      "show top available model per agent",            _cmd_best),
    "model":     SlashCmd("model",     "/model <agent> <model> — pin top model",        _cmd_model),
    "consensus": SlashCmd("consensus", "/consensus <prompt> — force 3-agent vote",      _cmd_consensus),
    "spawn":     SlashCmd("spawn",     "/spawn <agent> — pin all prompts to one agent", _cmd_spawn),
    "log":       SlashCmd("log",       "show this session's turns",                     _cmd_log),
    "init":      SlashCmd("init",      "write a starter PROJECT.md in cwd",             _cmd_init),
    "yolo":      SlashCmd("yolo",      "/yolo on|off — toggle skip-permissions",        _cmd_yolo),
    "learn":     SlashCmd("learn",     "/learn [kind] <text> — save a lesson",          _cmd_learn),
    "knowledge": SlashCmd("knowledge", "show accumulated lessons",                      _cmd_knowledge),
    "forget":    SlashCmd("forget",    "/forget <needle> — drop matching lessons",      _cmd_forget),
    "self":      SlashCmd("self",      "/self <instruction> — edit DigitalJulius itself", _cmd_self),
    "clear":     SlashCmd("clear",     "clear the screen",                              _cmd_clear),
    "quit":      SlashCmd("quit",      "exit DigitalJulius",                            _cmd_quit),
}


def dispatch(line: str, ctx: dict) -> bool:
    """Return True if `line` was a slash command and was dispatched."""
    if not line.startswith("/"):
        return False
    parts = line[1:].split()
    if not parts:
        return False
    name, *args = parts
    if name == "spawn" and args and args[0] == "off":
        ctx.pop("pinned_agent", None)
        ui.info("spawn unpinned")
        return True
    cmd = REGISTRY.get(name)
    if not cmd:
        ui.error(f"unknown command: /{name}. try /help")
        return True
    try:
        cmd.handler(ctx, *args)
    except TypeError:
        # handler doesn't take args
        cmd.handler(ctx)
    return True


def command_names() -> list[str]:
    return [f"/{c}" for c in REGISTRY] + ["/spawn off"]
