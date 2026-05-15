"""Slash-command registry."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from digitaljulius import secrets, ui
from digitaljulius.agents.registry import AGENTS, get_agent
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
from digitaljulius.log import log_path
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


def _cmd_switch(ctx, *args) -> None:
    """/switch                        — show available agents + current pin
    /switch <agent>                — pin every prompt to this agent
    /switch <agent> <model>        — pin agent and persist the model choice
    /switch off                    — unpin (return to auto-routing)
    """
    cfg = ctx["cfg"]

    if not args:
        pinned = ctx.get("pinned_agent") or "(auto-routing)"
        ui.console.print(f"[bold]current:[/bold] [magenta]{pinned}[/magenta]")
        ui.console.print("[bold cyan]available agents:[/bold cyan]")
        for name, ac in cfg["agents"].items():
            try:
                adapter = get_agent(name)
            except KeyError:
                continue
            installed = adapter.is_installed()
            authed = installed and adapter.is_authenticated()
            top = best_available_model(cfg, name) or "[red]exhausted[/red]"
            tag = "[green]✓[/green]" if authed else "[red]✗[/red]"
            ui.console.print(
                f"  {tag} [magenta]{name}[/magenta]  "
                f"top: [cyan]{top}[/cyan]  "
                f"[dim]chain: {' → '.join(ac.get('fallback_chain', []))}[/dim]"
            )
        ui.console.print(
            "[dim]usage: /switch <agent> [model]  •  /switch off to unpin[/dim]"
        )
        return

    if args[0].lower() in {"off", "auto", "none"}:
        ctx.pop("pinned_agent", None)
        ui.info("switch off — back to auto-routing")
        return

    agent = args[0]
    if agent not in cfg["agents"]:
        ui.error(f"unknown agent: {agent}")
        return
    try:
        adapter = get_agent(agent)
    except KeyError:
        ui.error(f"unknown agent: {agent}")
        return
    if not adapter.is_installed():
        ui.error(f"{agent} CLI not installed — `dj --login` or check `/auth`")
        return
    if not adapter.is_authenticated():
        ui.error(f"{agent} not authenticated — run `/auth {agent}`")
        return

    ctx["pinned_agent"] = agent

    if len(args) >= 2:
        model = args[1]
        cfg["agents"][agent]["top_model"] = model
        chain = cfg["agents"][agent].setdefault("fallback_chain", [])
        if model not in chain:
            chain.insert(0, model)
        save_config(cfg)
        ui.info(f"pinned to [magenta]{agent}[/magenta] · [cyan]{model}[/cyan] (saved)")
    else:
        top = best_available_model(cfg, agent) or "exhausted"
        ui.info(f"pinned to [magenta]{agent}[/magenta] · top model [cyan]{top}[/cyan]")


def _cmd_log(ctx, *args) -> None:
    """/log              — show this session's turns
    /log file         — print the runtime log file path
    /log tail [N]     — show last N (default 30) lines of the runtime log
    """
    sub = (args[0].lower() if args else "")
    if sub == "file":
        lp = log_path()
        if lp:
            ui.console.print(f"runtime log: [cyan]{lp}[/cyan]")
        else:
            ui.warn("logging not initialised")
        return
    if sub == "tail":
        lp = log_path()
        if not lp or not lp.exists():
            ui.warn("no runtime log yet")
            return
        try:
            n = int(args[1]) if len(args) > 1 else 30
        except ValueError:
            n = 30
        try:
            lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError as e:
            ui.error(f"could not read log: {e}")
            return
        for line in lines:
            ui.console.print(f"  [dim]{line}[/dim]")
        return

    session = ctx.get("session")
    if session is None:
        ui.warn("no session active")
        return
    ui.render_log(session.turns)


def _cmd_openai(ctx, *args) -> None:
    """/openai                   — show status (key set? top model? credits hint)
    /openai set-key <KEY>     — store OPENAI_API_KEY in the secret vault
    /openai model <MODEL>     — pin OpenAI's top_model (e.g. gpt-5, gpt-5-pro, o3-pro)
    /openai test              — fire a tiny ping at OpenAI to verify the key
    """
    cfg = ctx["cfg"]
    sub = (args[0].lower() if args else "")

    if not sub:
        has_key = secrets.has("OPENAI_API_KEY") or bool(__import__("os").environ.get("OPENAI_API_KEY"))
        oa = cfg.get("agents", {}).get("openai", {})
        cx = cfg.get("agents", {}).get("codex", {})
        ui.console.print("[bold cyan]OpenAI status[/bold cyan]")
        ui.console.print(f"  key set:    {'[green]yes[/green]' if has_key else '[red]no[/red]'}  "
                         f"[dim](secret OPENAI_API_KEY)[/dim]")
        ui.console.print(f"  top model:  [cyan]{oa.get('top_model', '?')}[/cyan]")
        ui.console.print(f"  fallback:   [dim]{' → '.join(oa.get('fallback_chain', []))}[/dim]")
        ui.console.print(f"  best now:   [cyan]{best_available_model(cfg, 'openai') or 'exhausted'}[/cyan]")
        ui.console.print()
        ui.console.print("[bold cyan]Codex CLI[/bold cyan]")
        try:
            adapter = get_agent("codex")
            installed = adapter.is_installed()
            authed = installed and adapter.is_authenticated()
        except Exception:
            installed = authed = False
        ui.console.print(f"  installed:  {'[green]yes[/green]' if installed else '[red]no[/red]'}")
        ui.console.print(f"  authed:     {'[green]yes[/green]' if authed else '[yellow]no[/yellow]'}")
        ui.console.print(f"  top model:  [cyan]{cx.get('top_model', '?')}[/cyan]")
        ui.console.print(f"  fallback:   [dim]{' → '.join(cx.get('fallback_chain', []))}[/dim]")
        ui.console.print()
        ui.console.print("[dim]usage: /openai set-key <KEY>  •  /openai model <MODEL>  •  /openai test[/dim]")
        return

    if sub == "set-key":
        if len(args) < 2:
            ui.warn("usage: /openai set-key <YOUR_OPENAI_API_KEY>")
            return
        key = args[1].strip()
        if not key.startswith("sk-"):
            ui.warn("key doesn't start with `sk-` — saving anyway, but double-check it")
        secrets.set_("OPENAI_API_KEY", key)
        ui.info("OPENAI_API_KEY stored in ~/.digitaljulius/secrets.json (chmod 600)")
        return

    if sub == "model":
        if len(args) < 2:
            ui.warn("usage: /openai model <MODEL>")
            return
        model = args[1]
        cfg["agents"]["openai"]["top_model"] = model
        chain = cfg["agents"]["openai"].setdefault("fallback_chain", [])
        if model not in chain:
            chain.insert(0, model)
        save_config(cfg)
        ui.info(f"openai top_model set to [cyan]{model}[/cyan] (saved)")
        return

    if sub == "test":
        adapter = get_agent("openai")
        if not adapter.is_authenticated():
            ui.error("no key — run `/openai set-key <KEY>` first")
            return
        model = cfg["agents"]["openai"].get("top_model", "gpt-5")
        ui.info(f"pinging openai · {model}…")
        resp = adapter.run("Reply with the single word: pong", model=model, timeout=30)
        if resp.ok:
            ui.info(f"OK ({resp.duration_s:.1f}s) — {resp.text[:120]}")
        else:
            ui.error(f"failed: {resp.stderr[:300]}")
        return

    ui.warn(f"unknown subcommand: /openai {sub} — try /openai with no args")


def _cmd_audit(ctx, *args) -> None:
    """/audit [agent] [text-or-last]  — full Claude<->OpenAI cross-critique.

      /audit               — auditor critiques the last turn, then the
                             original author rebuts. Two turns total.
      /audit openai        — force openai as auditor on last turn
      /audit codex <text>  — pipe explicit text to codex for critique

    Implements a 2-turn cross-critique:
      1) auditor reads original prompt + author's reply, returns critique
      2) original author reads critique, returns rebuttal/revision
    """
    session = ctx.get("session")
    if not session or not session.turns:
        ui.warn("no previous turn in this session to audit")
        return
    last = session.turns[-1]
    target = args[0].lower() if args else None

    # Default pair: if last turn was claude, audit with openai; else audit with claude.
    author = (last.chosen_agent or "").lower()
    if target is None:
        target = "openai" if author == "claude" else "claude"

    if target not in AGENTS:
        ui.error(f"unknown agent: {target} — choose one of {list(AGENTS)}")
        return
    auditor_adapter = get_agent(target)
    if not (auditor_adapter.is_installed() and auditor_adapter.is_authenticated()):
        ui.error(f"{target} is not authenticated — run /auth or /openai set-key")
        return

    extra = " ".join(args[1:]).strip()
    text_to_audit = extra or last.final_text or ""
    if not text_to_audit:
        ui.warn("nothing to audit — last turn had no final_text")
        return

    cfg = ctx["cfg"]
    cwd = ctx["cwd"]

    # ---- Turn 1: auditor critiques ----
    audit_prompt = (
        f"You are auditing another AI agent's response.\n"
        f"Original user prompt: {last.prompt!r}\n"
        f"Author: {last.chosen_agent or 'unknown'} ({last.chosen_model or '?'})\n\n"
        f"Their response:\n---\n{text_to_audit}\n---\n\n"
        f"Critique: 1) factual errors, 2) logic gaps, 3) anything you'd do "
        f"differently. Be terse — bullets only. End with VERDICT: SOUND / "
        f"CONCERNS / WRONG."
    )
    auditor_model = (
        best_available_model(cfg, target) or cfg["agents"][target].get("top_model")
    )
    ui.info(f"[1/2] auditing with [magenta]{target}[/magenta] · "
            f"[cyan]{auditor_model}[/cyan]…")
    audit_resp = auditor_adapter.run(audit_prompt, model=auditor_model,
                                     timeout=120, cwd=cwd)
    if not audit_resp.ok:
        ui.error(audit_resp.stderr[:400] or "audit failed")
        return
    ui.console.print(f"\n[bold]Audit by {target}:[/bold]")
    ui.render_response(audit_resp.text)

    # ---- Turn 2: original author rebuts ----
    if not author or author not in AGENTS:
        ui.warn("can't run rebuttal — original author unknown")
        return
    if author == target:
        ui.info("auditor == author, skipping rebuttal")
        return
    try:
        author_adapter = get_agent(author)
    except KeyError:
        return
    if not (author_adapter.is_installed() and author_adapter.is_authenticated()):
        ui.warn(f"original author {author} not available for rebuttal — skipping")
        return

    rebuttal_prompt = (
        f"You ({author}) previously answered this prompt:\n"
        f"---\n{last.prompt}\n---\n\n"
        f"Your answer was:\n---\n{text_to_audit}\n---\n\n"
        f"Another agent ({target}) audited it:\n---\n{audit_resp.text}\n---\n\n"
        f"Respond: which critiques are correct? Which are wrong or off-base? "
        f"If anything in your original answer needs revision, output the "
        f"revised version. Terse. End with FINAL: ACCEPT-ALL / ACCEPT-PARTIAL / "
        f"REJECT (with one-line reason)."
    )
    rebut_model = (
        best_available_model(cfg, author) or cfg["agents"][author].get("top_model")
    )
    ui.info(f"[2/2] rebuttal from [magenta]{author}[/magenta] · "
            f"[cyan]{rebut_model}[/cyan]…")
    rebut_resp = author_adapter.run(rebuttal_prompt, model=rebut_model,
                                    timeout=120, cwd=cwd)
    if rebut_resp.ok:
        ui.console.print(f"\n[bold]Rebuttal by {author}:[/bold]")
        ui.render_response(rebut_resp.text)
    else:
        ui.warn(f"rebuttal failed: {rebut_resp.stderr[:200]}")


def _cmd_plan(ctx, *args) -> None:
    """/plan <prompt> — force plan-then-execute mode for this prompt.

    Drafts a structured plan via the top-tier planner first, shows it with
    a +/- expand toggle, you approve/edit/abort, then it's executed.
    """
    text = " ".join(args).strip()
    if not text:
        ui.warn("usage: /plan <prompt> — forces plan-first even on simple tasks")
        return
    ctx["force_plan_first"] = True
    ctx["pending_prompt"] = text
    ctx["force_plan_run"] = True
    ui.info("plan-first mode armed for next prompt — running now…")


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
    "switch":    SlashCmd("switch",    "alias for /spawn; /switch shows available",     _cmd_switch),
    "log":       SlashCmd("log",       "/log [file|tail N] — turns, log path, or tail", _cmd_log),
    "openai":    SlashCmd("openai",    "/openai [set-key|model|test] — manage secondary boss", _cmd_openai),
    "audit":     SlashCmd("audit",     "/audit [agent] — 2-turn cross-critique (auditor + rebuttal)", _cmd_audit),
    "plan":      SlashCmd("plan",      "/plan <prompt> — force plan-then-execute with +/- expand toggle", _cmd_plan),
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
    if name == "switch" and args and args[0] == "off":
        ctx.pop("pinned_agent", None)
        ui.info("switch unpinned")
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
    return [f"/{c}" for c in REGISTRY] + ["/spawn off", "/switch off"]
