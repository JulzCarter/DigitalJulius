"""Main routing loop.

Pipeline per prompt:
    classify  ->  route  ->  (optional plan + approve)  ->  execute  ->  review

Tier behaviour:
    SIMPLE    single agent, no review
    MODERATE  single agent + advisory output review
    COMPLEX   2-agent consensus + advisory review
    CRITICAL  3-agent consensus + gatekeeper review (+ opt-in twin instance)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from digitaljulius.agents.base import AgentResponse
from digitaljulius.agents.registry import get_agent
from digitaljulius.approver import Verdict, review_output, review_plan
from digitaljulius.budget import best_available_model, record_call
from digitaljulius.complexity import Classification, Tier, classify
from digitaljulius.consensus import ConsensusResult, run_consensus, synthesise
from digitaljulius.knowledge import context_for_prompt


# A confirm callback returns True to proceed, False to skip.
ConfirmFn = Callable[[str], bool]


@dataclass
class RunResult:
    classification: Classification
    plan_verdict: Verdict | None = None
    responses: list[AgentResponse] = field(default_factory=list)
    output_verdict: Verdict | None = None
    final_text: str = ""
    chosen_agent: str = ""
    chosen_model: str = ""
    consensus: ConsensusResult | None = None
    skipped_reason: str = ""


def _pick_agents_for(tags: list[str], cfg: dict, max_n: int) -> list[str]:
    """Pick agents using the routing capability matrix, up to `max_n`."""
    routing = cfg.get("routing", {})
    seen: list[str] = []
    # Prefer agents listed for any of the prompt's tags, in tag order.
    for tag in tags or ["default"]:
        for agent in routing.get(tag, routing.get("default", [])):
            if agent not in seen:
                seen.append(agent)
    # Fall back to default for anything missing.
    for agent in routing.get("default", []):
        if agent not in seen:
            seen.append(agent)
    # Filter to enabled + installed + authenticated, preserving order.
    final: list[str] = []
    for agent in seen:
        if len(final) >= max_n:
            break
        if not cfg.get("agents", {}).get(agent, {}).get("enabled", True):
            continue
        try:
            adapter = get_agent(agent)
        except KeyError:
            continue
        if adapter.is_installed() and adapter.is_authenticated():
            final.append(agent)
    return final


def _single_agent_run(
    prompt: str,
    agent: str,
    cfg: dict,
    cwd: Path | None,
) -> AgentResponse:
    model = best_available_model(cfg, agent)
    if not model:
        return AgentResponse(
            agent=agent, model="-", ok=False, text="",
            stderr="all models exhausted for the day"
        )
    adapter = get_agent(agent)
    resp = adapter.run(prompt, model=model, yolo=True, cwd=cwd, timeout=300)
    record_call(agent, model)
    return resp


def run_prompt(
    prompt: str,
    cfg: dict,
    cwd: Path | None = None,
    confirm: ConfirmFn | None = None,
) -> RunResult:
    """Drive the full pipeline. `confirm` is called for CRITICAL twin-instance
    opt-in; it should return True to spin up a duplicate consensus run."""
    cwd = cwd or Path.cwd()

    classification = classify(prompt, cfg, cwd=cwd)
    result = RunResult(classification=classification)
    tier = classification.tier
    tags = classification.suggested_tags or ["default"]

    # Inject accumulated knowledge for non-SIMPLE prompts so the orchestrator
    # self-improves across sessions. Cheap turns skip this to save tokens.
    enriched_prompt = prompt
    if tier != Tier.SIMPLE:
        kb = context_for_prompt()
        if kb:
            enriched_prompt = f"{kb}\n\n---\n\n{prompt}"

    # ---- SIMPLE: one agent, no review --------------------------------------
    if tier == Tier.SIMPLE:
        agents = _pick_agents_for(tags, cfg, max_n=1)
        if not agents:
            result.skipped_reason = "no authenticated agent available"
            return result
        resp = _single_agent_run(enriched_prompt, agents[0], cfg, cwd)
        result.responses.append(resp)
        result.final_text = resp.text
        result.chosen_agent = resp.agent
        result.chosen_model = resp.model
        return result

    # ---- MODERATE: one agent + advisory review -----------------------------
    if tier == Tier.MODERATE:
        agents = _pick_agents_for(tags, cfg, max_n=1)
        if not agents:
            result.skipped_reason = "no authenticated agent available"
            return result
        resp = _single_agent_run(enriched_prompt, agents[0], cfg, cwd)
        result.responses.append(resp)
        result.final_text = resp.text
        result.chosen_agent = resp.agent
        result.chosen_model = resp.model
        if resp.ok and resp.text:
            result.output_verdict = review_output(prompt, resp.agent, resp.text, cfg, cwd)
        return result

    # ---- COMPLEX / CRITICAL: consensus -------------------------------------
    max_n = 2 if tier == Tier.COMPLEX else 3
    agents = _pick_agents_for(tags, cfg, max_n=max_n)
    if not agents:
        result.skipped_reason = "no authenticated agents available"
        return result

    # CRITICAL: optionally spin up a twin consensus run (same agents, second
    # pass) for redundancy. auto_spawn_twin defaults to False — prompts user.
    twin = False
    if tier == Tier.CRITICAL:
        if cfg.get("general", {}).get("auto_spawn_twin"):
            twin = True
        elif confirm is not None:
            twin = confirm(
                "This task is CRITICAL. Spawn a parallel twin-instance "
                "consensus run for redundancy? (y/N): "
            )

    # Plan-review for CRITICAL: ask the approver to vet the plan-of-attack
    # *before* execution. In gatekeeper mode, a BLOCK halts the run.
    if tier == Tier.CRITICAL:
        plan_summary = (
            f"Will run consensus across {agents} for this CRITICAL task. "
            f"Tags: {tags}. Twin instance: {twin}."
        )
        result.plan_verdict = review_plan(prompt, plan_summary, cfg, cwd)
        if (
            cfg.get("general", {}).get("approver_mode") == "gatekeeper"
            and not result.plan_verdict.approved
        ):
            result.skipped_reason = "plan blocked by approver"
            return result

    consensus = run_consensus(enriched_prompt, cfg, agents, cwd=cwd)
    if twin:
        twin_result = run_consensus(enriched_prompt, cfg, agents, cwd=cwd)
        consensus.responses.extend(twin_result.responses)

    result.consensus = consensus
    result.responses = list(consensus.responses)

    final = synthesise(prompt, consensus, cfg, cwd=cwd)
    result.final_text = final
    result.chosen_agent = consensus.chosen_agent
    result.chosen_model = consensus.chosen_model

    if final:
        result.output_verdict = review_output(
            prompt, result.chosen_agent or "consensus", final, cfg, cwd
        )
        if (
            tier == Tier.CRITICAL
            and cfg.get("general", {}).get("approver_mode") == "gatekeeper"
            and not result.output_verdict.approved
        ):
            # Surface the block but keep the text so the user can still see it.
            result.skipped_reason = "output blocked by approver"

    return result
