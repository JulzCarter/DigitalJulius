"""Rich-based renderers for the DigitalJulius TUI."""
from __future__ import annotations

from typing import Iterable

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from digitaljulius.auth import AuthStatus
from digitaljulius.budget import status_table

console = Console()


def banner() -> None:
    title = Text("DigitalJulius", style="bold cyan")
    sub = Text(
        "free-tier orchestrator across Claude / Gemini / GitHub",
        style="dim",
    )
    console.print(Panel.fit(title + Text("\n") + sub, border_style="cyan"))


def status_line(agent: str, model: str, tier: str, yolo: bool) -> None:
    mode = "[bold green]YOLO[/bold green]" if yolo else "[yellow]safe[/yellow]"
    console.print(
        f"[dim]» tier=[/dim][bold]{tier}[/bold] "
        f"[dim]agent=[/dim][magenta]{agent}[/magenta] "
        f"[dim]model=[/dim][cyan]{model}[/cyan] "
        f"[dim]mode=[/dim]{mode}"
    )


def render_response(text: str) -> None:
    if not text:
        console.print("[dim italic](no response)[/dim italic]")
        return
    try:
        console.print(Markdown(text))
    except Exception:
        console.print(text)


def render_verdict(label: str, verdict) -> None:
    if verdict is None:
        return
    color = "green" if verdict.approved else "red"
    head = f"[{color}]{label}: {'OK' if verdict.approved else 'BLOCK'}[/{color}]"
    console.print(head)
    if verdict.critique:
        console.print(f"  [dim]{verdict.critique}[/dim]")
    for s in verdict.suggestions[:3]:
        console.print(f"  [dim]- {s}[/dim]")


def render_auth(probes: Iterable[AuthStatus]) -> None:
    t = Table(title="Agent auth", show_lines=False)
    t.add_column("agent", style="bold")
    t.add_column("installed")
    t.add_column("authenticated")
    t.add_column("note", style="dim")
    for p in probes:
        t.add_row(
            p.agent,
            "yes" if p.installed else "[red]no[/red]",
            "yes" if p.authenticated else "[red]no[/red]",
            p.note,
        )
    console.print(t)


def render_budget(cfg: dict) -> None:
    rows = status_table(cfg)
    t = Table(title="Daily budget", show_lines=False)
    t.add_column("agent", style="bold")
    t.add_column("model")
    t.add_column("used", justify="right")
    t.add_column("cap", justify="right")
    t.add_column("%", justify="right")
    for r in rows:
        pct = r["pct"]
        pct_str = f"{pct * 100:.0f}%"
        if pct >= 0.9:
            pct_str = f"[red]{pct_str}[/red]"
        elif pct >= 0.75:
            pct_str = f"[yellow]{pct_str}[/yellow]"
        t.add_row(r["agent"], r["model"], str(r["used"]), str(r["cap"]), pct_str)
    console.print(t)


def render_agents(cfg: dict) -> None:
    t = Table(title="Configured agents", show_lines=False)
    t.add_column("name", style="bold")
    t.add_column("command")
    t.add_column("top model")
    t.add_column("fallback chain", style="dim")
    for name, ac in cfg["agents"].items():
        t.add_row(
            name,
            ac.get("command", name),
            ac.get("top_model", "-"),
            " → ".join(ac.get("fallback_chain", [])),
        )
    console.print(t)


def render_routing(cfg: dict) -> None:
    t = Table(title="Capability routing", show_lines=False)
    t.add_column("tag", style="bold")
    t.add_column("preference order")
    for tag, agents in cfg["routing"].items():
        t.add_row(tag, " → ".join(agents))
    console.print(t)


def render_log(turns: list) -> None:
    if not turns:
        console.print("[dim]no turns yet this session[/dim]")
        return
    t = Table(title=f"Session log ({len(turns)} turn{'s' if len(turns)!=1 else ''})")
    t.add_column("#", justify="right")
    t.add_column("tier")
    t.add_column("agent")
    t.add_column("model")
    t.add_column("prompt", style="dim")
    for i, turn in enumerate(turns, 1):
        t.add_row(
            str(i),
            turn.tier,
            turn.chosen_agent or "-",
            turn.chosen_model or "-",
            (turn.prompt[:60] + "…") if len(turn.prompt) > 60 else turn.prompt,
        )
    console.print(t)


def info(msg: str) -> None:
    console.print(f"[cyan]ℹ[/cyan] {msg}")


def warn(msg: str) -> None:
    console.print(f"[yellow]⚠[/yellow] {msg}")


def error(msg: str) -> None:
    console.print(f"[red]✖[/red] {msg}")
