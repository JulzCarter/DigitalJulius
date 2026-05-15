"""Quota-aware single-agent execution helpers."""
from __future__ import annotations

import time
from pathlib import Path

from digitaljulius.agents.base import AgentResponse
from digitaljulius.agents.registry import get_agent
from digitaljulius.budget import best_available_model, exhaust_model, record_call
from digitaljulius.events import Reporter, StepEvent
from digitaljulius.log import get_logger

log = get_logger(__name__)


def _looks_like_quota(resp: AgentResponse, adapter) -> bool:
    """Detect quota/credit failures using adapter-specific and generic checks."""
    if hasattr(adapter, "is_quota_error") and adapter.is_quota_error(resp):
        return True
    blob = ((resp.stderr or "") + " " + (resp.text or "")).lower()
    needles = (
        "out of credits", "insufficient credits", "insufficient funds",
        "credit balance", "credit balance is too low", "credits required",
        "quota exceeded", "rate limit", "hit your limit", "usage limit",
        "billing", "payment required", "402 ", "429 ",
    )
    return any(n in blob for n in needles)


def _single_agent_run(
    prompt: str,
    agent: str,
    cfg: dict,
    cwd: Path | None,
    on_event: Reporter,
) -> AgentResponse:
    """Run `agent`, falling back through its model chain on quota errors.

    Returns the first successful AgentResponse. If every model in this agent's
    chain hits quota, returns the last failure with stderr == "QUOTA_EXCEEDED"
    so the caller can rotate to the next agent.
    """
    auto_fallback = cfg.get("general", {}).get("auto_fallback", True)
    last_resp: AgentResponse | None = None

    while True:
        model = best_available_model(cfg, agent)
        if not model:
            log.info("single_agent.agent_skip agent=%s reason=all_models_exhausted", agent)
            on_event(StepEvent(
                kind="agent_skip", label=f"{agent} skipped",
                agent=agent, note="all models exhausted for the day",
            ))
            return last_resp or AgentResponse(
                agent=agent, model="-", ok=False, text="",
                stderr="QUOTA_EXCEEDED",
            )

        adapter = get_agent(agent)
        log.info("single_agent.agent_start agent=%s model=%s prompt_chars=%d",
                 agent, model, len(prompt))
        on_event(StepEvent(
            kind="agent_start", label=f"{agent} generating",
            agent=agent, model=model,
        ))
        t0 = time.time()
        resp = adapter.run(prompt, model=model, yolo=True, cwd=cwd, timeout=300)
        dur = time.time() - t0

        if not resp.ok and _looks_like_quota(resp, adapter):
            log.warning("single_agent.quota agent=%s model=%s err=%s",
                        agent, model, (resp.stderr or "")[:160])
            exhaust_model(agent, model)
            on_event(StepEvent(
                kind="agent_fail", label=f"{agent} quota/credits hit",
                agent=agent, model=model, duration_s=dur,
                note=f"falling back to next model in {agent}'s chain",
            ))
            resp.stderr = "QUOTA_EXCEEDED"
            last_resp = resp
            if auto_fallback:
                continue
            return resp

        if resp.ok:
            log.info("single_agent.agent_done agent=%s model=%s dur=%.1fs out_chars=%d",
                     agent, model, dur, len(resp.text or ""))
            record_call(agent, model)
            on_event(StepEvent(
                kind="agent_done", label=f"{agent} done",
                agent=agent, model=model, duration_s=dur,
            ))
            return resp

        log.error("single_agent.agent_fail agent=%s model=%s err=%s",
                  agent, model, (resp.stderr or "")[:200])
        on_event(StepEvent(
            kind="agent_fail", label=f"{agent} failed",
            agent=agent, model=model, duration_s=dur,
            note=(resp.stderr or "")[:200],
        ))
        return resp
