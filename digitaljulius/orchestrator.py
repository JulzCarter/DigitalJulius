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
from digitaljulius.events import Reporter, StepEvent, silent
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
    on_event: Reporter,
) -> AgentResponse:
    import time
    model = best_available_model(cfg, agent)
    if not model:
        on_event(StepEvent(
            kind="agent_skip", label=f"{agent} skipped",
            agent=agent, note="all models exhausted for the day",
        ))
        return AgentResponse(
            agent=agent, model="-", ok=False, text="",
            stderr="all models exhausted for the day"
        )
    adapter = get_agent(agent)
    on_event(StepEvent(
        kind="agent_start", label=f"{agent} generating",
        agent=agent, model=model,
    ))
    t0 = time.time()
    resp = adapter.run(prompt, model=model, yolo=True, cwd=cwd, timeout=300)
    record_call(agent, model)
    on_event(StepEvent(
        kind="agent_done" if resp.ok else "agent_fail",
        label=f"{agent} {'done' if resp.ok else 'failed'}",
        agent=agent, model=model, duration_s=time.time() - t0,
        note="" if resp.ok else (resp.stderr or "")[:200],
    ))
    return resp


def run_prompt(
    prompt: str,
    cfg: dict,
    cwd: Path | None = None,
    confirm: ConfirmFn | None = None,
    on_event: Reporter | None = None,
) -> RunResult:
    """Drive the full pipeline. `confirm` is called for CRITICAL twin-instance
    opt-in; it should return True to spin up a duplicate consensus run."""
    import time
    cwd = cwd or Path.cwd()
    on_event = on_event or silent

    classifier_cfg = cfg.get("classifier", {})
    on_event(StepEvent(
        kind="classify_start", label="classifying complexity",
        agent=classifier_cfg.get("agent", "gemini"),
        model=classifier_cfg.get("model", "gemini-2.5-flash"),
    ))
    t0 = time.time()
    classification = classify(prompt, cfg, cwd=cwd)
    result = RunResult(classification=classification)
    tier = classification.tier
    tags = classification.suggested_tags or ["default"]
    on_event(StepEvent(
        kind="classify_done",
        label=f"tier={tier.value}  tags={tags}",
        duration_s=time.time() - t0,
        note=classification.reason,
    ))

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
        resp = _single_agent_run(enriched_prompt, agents[0], cfg, cwd, on_event)
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
        resp = _single_agent_run(enriched_prompt, agents[0], cfg, cwd, on_event)
        result.responses.append(resp)
        result.final_text = resp.text
        result.chosen_agent = resp.agent
        result.chosen_model = resp.model
        if resp.ok and resp.text:
            approver_cfg = cfg.get("approver", {})
            on_event(StepEvent(
                kind="review_start", label="reviewing output",
                agent=approver_cfg.get("agent", "claude"),
                model=approver_cfg.get("model", "opus"),
            ))
            t1 = time.time()
            result.output_verdict = review_output(prompt, resp.agent, resp.text, cfg, cwd)
            on_event(StepEvent(
                kind="review_done",
                label=f"output review: {'OK' if result.output_verdict.approved else 'BLOCK'}",
                duration_s=time.time() - t1,
                note=result.output_verdict.critique[:200] if result.output_verdict.critique else "",
            ))
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

    on_event(StepEvent(
        kind="route_done",
        label=f"routing to {agents}" + (" + twin" if twin else ""),
    ))

    approver_cfg = cfg.get("approver", {})
    # Plan-review for CRITICAL: ask the approver to vet the plan-of-attack
    # *before* execution. In gatekeeper mode, a BLOCK halts the run.
    if tier == Tier.CRITICAL:
        plan_summary = (
            f"Will run consensus across {agents} for this CRITICAL task. "
            f"Tags: {tags}. Twin instance: {twin}."
        )
        on_event(StepEvent(
            kind="plan_review_start", label="reviewing plan",
            agent=approver_cfg.get("agent", "claude"),
            model=approver_cfg.get("model", "opus"),
        ))
        t1 = time.time()
        result.plan_verdict = review_plan(prompt, plan_summary, cfg, cwd)
        on_event(StepEvent(
            kind="plan_review_done",
            label=f"plan review: {'OK' if result.plan_verdict.approved else 'BLOCK'}",
            duration_s=time.time() - t1,
        ))
        if (
            cfg.get("general", {}).get("approver_mode") == "gatekeeper"
            and not result.plan_verdict.approved
        ):
            result.skipped_reason = "plan blocked by approver"
            return result

    consensus = run_consensus(enriched_prompt, cfg, agents, cwd=cwd, on_event=on_event)
    if twin:
        on_event(StepEvent(kind="twin_start", label="running twin consensus pass"))
        twin_result = run_consensus(enriched_prompt, cfg, agents, cwd=cwd, on_event=on_event)
        consensus.responses.extend(twin_result.responses)

    result.consensus = consensus
    result.responses = list(consensus.responses)

    on_event(StepEvent(
        kind="synth_start", label="synthesising final answer",
        agent=approver_cfg.get("agent", "claude"),
        model=approver_cfg.get("model", "opus"),
    ))
    t1 = time.time()
    final = synthesise(prompt, consensus, cfg, cwd=cwd)
    result.final_text = final
    result.chosen_agent = consensus.chosen_agent
    result.chosen_model = consensus.chosen_model
    on_event(StepEvent(
        kind="synth_done",
        label=f"synthesis via {result.chosen_agent or 'consensus'}",
        duration_s=time.time() - t1,
    ))

    if final:
        on_event(StepEvent(
            kind="review_start", label="reviewing output",
            agent=approver_cfg.get("agent", "claude"),
            model=approver_cfg.get("model", "opus"),
        ))
        t1 = time.time()
        result.output_verdict = review_output(
            prompt, result.chosen_agent or "consensus", final, cfg, cwd
        )
        on_event(StepEvent(
            kind="review_done",
            label=f"output review: {'OK' if result.output_verdict.approved else 'BLOCK'}",
            duration_s=time.time() - t1,
            note=result.output_verdict.critique[:200] if result.output_verdict.critique else "",
        ))
        if (
            tier == Tier.CRITICAL
            and cfg.get("general", {}).get("approver_mode") == "gatekeeper"
            and not result.output_verdict.approved
        ):
            # Surface the block but keep the text so the user can still see it.
            result.skipped_reason = "output blocked by approver"

    return result
