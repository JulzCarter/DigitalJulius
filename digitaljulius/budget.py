"""Per-agent per-model daily request counter + tier fallback.

Mirrors the design of ~/.gemini/hooks/quota_guard.py but tracks ALL three
agents in one place (~/.digitaljulius/budget.json).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from digitaljulius.config import BUDGET_PATH, ensure_dirs


def _today() -> str:
    return date.today().isoformat()


def load_budget() -> dict:
    ensure_dirs()
    if BUDGET_PATH.exists():
        try:
            data = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
            if data.get("date") == _today():
                return data
        except Exception:
            pass
    return {"date": _today(), "counts": {}}


def save_budget(data: dict) -> None:
    ensure_dirs()
    BUDGET_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def key(agent: str, model: str) -> str:
    return f"{agent}:{model}"


def record_call(agent: str, model: str) -> dict:
    data = load_budget()
    k = key(agent, model)
    data["counts"][k] = data["counts"].get(k, 0) + 1
    save_budget(data)
    return data


def exhaust_model(agent: str, model: str) -> None:
    """Manually mark a model as exhausted (e.g. after a quota error)."""
    data = load_budget()
    k = key(agent, model)
    # We set it to a high value so usage_pct > switch_pct
    data["counts"][k] = 9999
    save_budget(data)


def usage_pct(cfg: dict, agent: str, model: str) -> float:
    data = load_budget()
    used = data["counts"].get(key(agent, model), 0)
    cap = cfg["budget"]["daily_caps"].get(key(agent, model)) or 1000
    return used / cap if cap else 0.0


def best_available_model(cfg: dict, agent: str) -> str | None:
    """Return the highest-tier model for `agent` that is still under switch_pct.
    None if every model in that agent's chain is exhausted."""
    chain = cfg["agents"][agent]["fallback_chain"]
    switch = float(cfg["budget"]["switch_pct"])
    for model in chain:
        if usage_pct(cfg, agent, model) < switch:
            return model
    return None


def status_table(cfg: dict) -> list[dict]:
    """Build a list of rows for /budget rendering."""
    rows = []
    data = load_budget()
    caps = cfg["budget"]["daily_caps"]
    for agent_name, agent_cfg in cfg["agents"].items():
        for model in agent_cfg["fallback_chain"]:
            k = key(agent_name, model)
            used = data["counts"].get(k, 0)
            cap = caps.get(k) or 1000
            rows.append({
                "agent": agent_name,
                "model": model,
                "used": used,
                "cap": cap,
                "pct": used / cap if cap else 0,
            })
    return rows
