"""User-locked directives applied to every agent call.

Two rules the user has marked NON-NEGOTIABLE:

1.  Every worker agent receives an EXECUTION_DIRECTIVE prefix so it does the
    work instead of returning instructions for the user to follow manually.
2.  Planning roles (approver, synthesiser, plan-reviewer) only ever run on
    TOP-TIER models. If the configured top-tier model is unavailable across
    every boss agent, the orchestrator must prompt the user to pick — never
    silently downgrade to haiku / mini / nano.

Centralised here so a future user preference change touches one file.
"""
from __future__ import annotations

# Prepended to every prompt that goes through the orchestrator. Worker
# agents see this as the leading section before the user's actual prompt.
EXECUTION_DIRECTIVE = """[USER STANDING ORDERS — apply to every response]

1. EXECUTE, do not instruct. When a task can be done, do it. Edit the files,
   run the commands, build the artifacts. Do not return a list of steps for
   the user to perform manually unless manual steps are 100% required (e.g.
   credentials only the user can supply, hardware actions, account-bound
   browser flows).

2. When you do something, give the user concrete artifacts: file paths, URLs,
   commit hashes, screenshots — whatever lets them inspect the work without
   re-doing it. No "you can now…" phrasing; show what already happened.

3. If a task is genuinely ambiguous, ask ONE clarifying question — don't
   pre-emptively split into manual steps "just in case".

4. Be terse. The user reads diffs faster than prose.

[END USER STANDING ORDERS]
"""


# Boss-tier agents in priority order. Only their TOP_MODEL is acceptable
# for planning. Order = preference when the primary boss is exhausted.
PLANNING_BOSS_AGENTS = ["claude", "openai", "codex"]


def wrap_for_execution(prompt: str) -> str:
    """Prepend the standing orders to a worker prompt."""
    if not prompt:
        return EXECUTION_DIRECTIVE
    if EXECUTION_DIRECTIVE in prompt:
        return prompt  # already wrapped (e.g. nested role call)
    return EXECUTION_DIRECTIVE + "\n\n" + prompt


def top_tier_planning_chain(cfg: dict) -> list[tuple[str, str]]:
    """Return [(agent, top_model)] for each boss agent that has its top model
    configured. Order matches PLANNING_BOSS_AGENTS."""
    chain: list[tuple[str, str]] = []
    agents_cfg = cfg.get("agents", {})
    for agent in PLANNING_BOSS_AGENTS:
        ac = agents_cfg.get(agent)
        if not ac or not ac.get("enabled", True):
            continue
        top = ac.get("top_model")
        if top:
            chain.append((agent, top))
    return chain


def downgrade_options(cfg: dict) -> list[tuple[str, str]]:
    """When every top-tier planner is exhausted, this is the menu we offer
    the user — the SECOND entry in each boss agent's fallback_chain. The
    user must explicitly approve any of these before they run."""
    options: list[tuple[str, str]] = []
    agents_cfg = cfg.get("agents", {})
    for agent in PLANNING_BOSS_AGENTS:
        ac = agents_cfg.get(agent)
        if not ac or not ac.get("enabled", True):
            continue
        chain = ac.get("fallback_chain") or []
        # Skip the top_model (already tried); offer next 2 entries.
        skip = ac.get("top_model")
        for m in chain:
            if m == skip:
                continue
            options.append((agent, m))
            if len([o for o in options if o[0] == agent]) >= 2:
                break
    return options
