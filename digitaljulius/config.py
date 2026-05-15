"""Config and runtime-state paths."""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import tomllib  # py3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

import tomli_w

# Force UTF-8 on Windows consoles so emoji and box-drawing don't crash.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

HOME = Path(os.path.expanduser("~"))
DJ_HOME = HOME / ".digitaljulius"
CONFIG_PATH = DJ_HOME / "config.toml"
STATE_PATH = DJ_HOME / "state.json"
BUDGET_PATH = DJ_HOME / "budget.json"
LOG_DIR = DJ_HOME / "logs"

DEFAULT_CONFIG = {
    "general": {
        "yolo_default": True,
        "approver_mode": "advisory",  # "advisory" or "gatekeeper"
        "auto_spawn_twin": False,     # ask before twin-instance
        "auto_learn": True,           # distil a lesson after each non-SIMPLE turn
        # When True, a quota/credit error on the chosen agent automatically
        # marks its current model exhausted, tries the next model in the chain,
        # and finally rotates to the next agent in the routing order.
        "auto_fallback": True,
    },
    "agents": {
        "claude": {
            "enabled": True,
            "command": "claude",
            "top_model": "opus",
            "fallback_chain": ["opus", "sonnet", "haiku"],
        },
        # Secondary boss. When Claude credits run out the orchestrator
        # immediately escalates here (top of openai chain) rather than
        # walking down the worker tier. See orchestrator._escalate_to_openai.
        "openai": {
            "enabled": True,
            "command": "openai",
            "top_model": "gpt-5",
            "fallback_chain": [
                "gpt-5",
                "gpt-5-pro",
                "o3-pro",
                "gpt-5-mini",
                "gpt-4.1",
                "gpt-4o",
                "gpt-4o-mini",
            ],
        },
        # Deep-coding worker. Routed to for COMPLEX/CRITICAL coding tasks.
        # Codex CLI handles its own internal sub-agent spawning.
        "codex": {
            "enabled": True,
            "command": "codex",
            "top_model": "gpt-5-codex",
            "fallback_chain": [
                "gpt-5-codex",
                "gpt-5",
                "gpt-5-mini",
                "o3-pro",
                "gpt-4.1",
            ],
        },
        "gemini": {
            "enabled": True,
            "command": "gemini",
            "top_model": "gemini-2.5-pro",
            "fallback_chain": [
                "gemini-2.5-pro",
                "gemini-2.5-flash",
                "gemini-2.5-flash-lite",
                "gemini-2.0-flash",
                "gemini-1.5-flash",
            ],
        },
        "github": {
            "enabled": True,
            "command": "gh",
            # All models here are free-tier accessible via `gh models run`.
            # The paid-only ones (gpt-5*, o-series, grok-*) are intentionally
            # excluded — they 403 / "unavailable_model" without GH Copilot.
            "top_model": "openai/gpt-4.1",
            "fallback_chain": [
                "openai/gpt-4.1",
                "openai/gpt-4o",
                "meta/llama-3.3-70b-instruct",
                "mistral-ai/mistral-medium-2505",
                "openai/gpt-4.1-mini",
                "openai/gpt-4o-mini",
                "mistral-ai/mistral-small-2503",
                "microsoft/phi-4",
                "deepseek/deepseek-v3-0324",
            ],
        },
    },
    "budget": {
        # Conservative free-tier daily request caps.
        # Tune in ~/.digitaljulius/config.toml as quotas evolve.
        "warn_pct": 0.75,
        "switch_pct": 0.90,
        "daily_caps": {
            "claude:opus": 100,
            "claude:sonnet": 500,
            "claude:haiku": 1000,
            # OpenAI: $250 in API credits + ChatGPT Pro 20x for Codex.
            # Generous caps so we don't hold back from the secondary boss.
            "openai:gpt-5": 500,
            "openai:gpt-5-pro": 200,
            "openai:o3-pro": 200,
            "openai:gpt-5-mini": 1000,
            "openai:gpt-4.1": 800,
            "openai:gpt-4o": 800,
            "openai:gpt-4o-mini": 1500,
            # Codex CLI usage is bounded by ChatGPT Pro 20x weekly limit;
            # we soft-cap per day to spread the load.
            "codex:gpt-5-codex": 300,
            "codex:gpt-5": 200,
            "codex:gpt-5-mini": 500,
            "codex:o3-pro": 100,
            "codex:gpt-4.1": 300,
            "gemini:gemini-2.5-pro": 1000,
            "gemini:gemini-2.5-flash": 1500,
            "gemini:gemini-2.5-flash-lite": 2000,
            "gemini:gemini-2.0-flash": 1500,
            "gemini:gemini-1.5-flash": 50,
            # GitHub Models free-tier per-model daily caps (rough — adjust
            # in ~/.digitaljulius/config.toml as you see real limits).
            "github:openai/gpt-4.1": 150,
            "github:openai/gpt-4o": 150,
            "github:meta/llama-3.3-70b-instruct": 150,
            "github:mistral-ai/mistral-medium-2505": 150,
            "github:openai/gpt-4.1-mini": 200,
            "github:openai/gpt-4o-mini": 200,
            "github:mistral-ai/mistral-small-2503": 200,
            "github:microsoft/phi-4": 200,
            "github:deepseek/deepseek-v3-0324": 150,
        },
    },
    "routing": {
        # Capability matrix — agent priority by task tag.
        # First entry is preferred; subsequent are fallbacks. Claude is
        # always primary; OpenAI is always second so credits-exhaustion on
        # Claude rotates straight to OpenAI's top-tier model rather than
        # stepping down through worker agents.
        "architecture": ["claude", "openai", "codex", "gemini", "github"],
        "refactor":     ["claude", "openai", "codex", "github", "gemini"],
        "quick_edit":   ["github", "openai", "gemini", "claude", "codex"],
        # Gemini's long-context claim doesn't hold in practice — it drops
        # files it just read. Claude leads, then OpenAI's gpt-5 (1M+ context).
        "long_context": ["claude", "openai", "github", "gemini", "codex"],
        "web_search":   ["openai", "gemini", "claude", "github", "codex"],
        "math":         ["openai", "claude", "codex", "gemini", "github"],
        # Deep-coding tier: Codex first (it spawns sub-agents internally),
        # then Claude Code, then OpenAI direct.
        "deep_coding":  ["codex", "claude", "openai", "gemini", "github"],
        # Meta-modification — anything that asks DigitalJulius to change
        # itself (logging, display, routing, prompts). Claude only:
        # the source-map context lives in CLAUDE.md, and Gemini/Codex
        # would hallucinate from local files instead.
        "self_modify":  ["claude"],
        "default":      ["claude", "openai", "codex", "gemini", "github"],
    },
    # Claude owns the three orchestrator-level roles: planner (approver),
    # router (classifier), and quality-checker / merger (synthesizer). Gemini
    # has been observed losing context even when re-reading source files, so
    # it's intentionally NOT trusted with any role that decides what other
    # agents do — it stays a worker only.
    "approver": {
        "agent": "claude",
        "model": "opus",
    },
    "classifier": {
        # Haiku: cheap + fast (~2-4s) and respects Claude's stronger
        # context-tracking. Avoids the 10-15s/Gemini-quota stall.
        "agent": "claude",
        "model": "haiku",
    },
    "synthesizer": {
        "agent": "claude",
        "model": "opus",
    },
    "memory": {
        "history_turns": 4,
        "history_chars": 3500,
        "project_chars": 4000,
    },
}


def ensure_dirs() -> None:
    DJ_HOME.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    with CONFIG_PATH.open("rb") as f:
        loaded = tomllib.load(f)
    loaded = _migrate(loaded)
    # Merge with defaults so missing keys don't break things.
    merged = _deep_merge(DEFAULT_CONFIG, loaded)
    # Persist if migration changed anything so we don't keep re-migrating.
    save_config(merged)
    return merged


# Agents that were removed from the registry but may still linger in older
# config.toml files written by previous DigitalJulius versions.
_REMOVED_AGENTS = {"qwen"}

# Agents added since older configs — _migrate() ensures they get pulled into
# routing tables and budget caps so existing users don't have to nuke their
# config to pick up OpenAI / Codex.
_NEW_AGENTS = {"openai", "codex"}


def _migrate(loaded: dict) -> dict:
    """Drop references to retired agents and refresh agent blocks that look
    like stubs from a partially-completed swap."""
    agents = loaded.get("agents") or {}
    for dead in _REMOVED_AGENTS & set(agents):
        agents.pop(dead, None)

    # If github's saved chain references any paid-tier-only model, the user
    # got a stub config from an earlier dev iteration — reset to defaults.
    # These names are 403/unavailable on a plain GitHub free account, so
    # leaving them in the chain bricks the fallback ladder.
    PAID_ONLY = {
        "openai/gpt-5", "openai/gpt-5-mini", "openai/gpt-5-chat",
        "openai/gpt-5-nano", "openai/o1", "openai/o1-mini",
        "openai/o3", "openai/o3-mini", "openai/o4-mini",
        "xai/grok-3", "xai/grok-3-mini",
    }
    gh = agents.get("github")
    if gh:
        chain = gh.get("fallback_chain") or []
        if (
            len(chain) < 6
            or gh.get("top_model") in PAID_ONLY
            or any(m in PAID_ONLY for m in chain)
        ):
            agents["github"] = dict(DEFAULT_CONFIG["agents"]["github"])

    # Routing tables: strip any agent name not in the live registry-shaped
    # default set, and make sure every live agent appears somewhere in each
    # tag's fallback list so it can actually be reached. NEW agents (openai,
    # codex) get *spliced in at position 1* rather than appended, so Claude's
    # secondary boss escalation works for users who never reset their config.
    live = list(DEFAULT_CONFIG["agents"].keys())
    routing = loaded.get("routing") or {}
    # Make sure every default tag exists, including the new `deep_coding` one.
    for tag, default_chain in DEFAULT_CONFIG["routing"].items():
        if tag not in routing:
            routing[tag] = list(default_chain)
    for tag, chain in list(routing.items()):
        pruned = [a for a in (chain or []) if a in live]
        # self_modify is intentionally claude-only — never backfill workers.
        if tag == "self_modify":
            routing[tag] = ["claude"] if "claude" in live else pruned
            continue
        # Splice newly-added agents at index 1 (right after the primary boss).
        for new_agent in _NEW_AGENTS:
            if new_agent in live and new_agent not in pruned:
                if len(pruned) >= 1:
                    pruned.insert(1, new_agent)
                else:
                    pruned.append(new_agent)
        # Append anything else that's missing.
        for live_agent in live:
            if live_agent not in pruned:
                pruned.append(live_agent)
        routing[tag] = pruned
    loaded["routing"] = routing

    # Budget caps: backfill any missing default-cap entries (especially the
    # new openai:* and codex:* ones) without overwriting user customisations.
    budget = loaded.setdefault("budget", {})
    caps = budget.setdefault("daily_caps", {})
    for k, v in DEFAULT_CONFIG["budget"]["daily_caps"].items():
        if k not in caps:
            caps[k] = v

    # Agent blocks: backfill missing agents (openai, codex) entirely.
    agents = loaded.setdefault("agents", {})
    for new_agent in _NEW_AGENTS:
        if new_agent not in agents:
            agents[new_agent] = dict(DEFAULT_CONFIG["agents"][new_agent])

    # Budget caps: remove keys whose agent prefix is gone.
    for k in list(caps.keys()):
        agent_prefix = k.split(":", 1)[0]
        if agent_prefix in _REMOVED_AGENTS:
            caps.pop(k, None)

    # long_context: if existing chain still leads with gemini, fix it —
    # Gemini drops context in practice, Claude is more reliable here.
    lc = routing.get("long_context") or []
    if lc and lc[0] == "gemini":
        routing["long_context"] = list(DEFAULT_CONFIG["routing"]["long_context"])

    # Role assignments: force Claude to own classifier/approver/synthesiser.
    # Older configs pointed classifier at gemini-flash; Gemini has been
    # demoted to worker-only since it drops orchestration context.
    classifier = loaded.get("classifier") or {}
    if classifier.get("agent") != "claude":
        loaded["classifier"] = dict(DEFAULT_CONFIG["classifier"])
    synth = loaded.get("synthesizer") or {}
    if not synth.get("agent"):
        loaded["synthesizer"] = dict(DEFAULT_CONFIG["synthesizer"])
    approver = loaded.get("approver") or {}
    if approver.get("agent") != "claude":
        loaded["approver"] = dict(DEFAULT_CONFIG["approver"])

    return loaded


def save_config(cfg: dict) -> None:
    ensure_dirs()
    with CONFIG_PATH.open("wb") as f:
        tomli_w.dump(cfg, f)


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result
