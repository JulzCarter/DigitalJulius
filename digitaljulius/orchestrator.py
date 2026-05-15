"""Main routing loop.

Routes a prompt to the right agent(s) given a complexity tier and falls back
gracefully when any agent runs out of credits or hits a daily quota.

The fallback chain has two levels:

  1. **Same-agent fallback.** If `claude:opus` returns a credit/quota error
     we mark that model exhausted in budget.json and immediately retry on
     `claude:sonnet`, then `claude:haiku`, etc.
  2. **Cross-agent fallback.** If every model for the current agent is
     exhausted we move on to the next agent in the routing order.

This way "Claude ran out of credits" is silently absorbed and the user gets
an answer from Gemini or GitHub Models without re-running the prompt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from digitaljulius.agents.base import AgentResponse
from digitaljulius.agents.registry import get_agent
from digitaljulius.approver import Verdict, review_output, review_plan
from digitaljulius.budget import best_available_model
from digitaljulius.complexity import Classification, Tier, classify
from digitaljulius.consensus import ConsensusResult, run_consensus, synthesise
from digitaljulius.core_directives import wrap_for_execution
from digitaljulius.events import Reporter, StepEvent, silent
from digitaljulius.knowledge import context_for_prompt
from digitaljulius.log import get_logger
from digitaljulius.planning import Plan, draft_plan, plan_to_worker_prefix
from digitaljulius.progress_reporter import ProgressReporter
from digitaljulius.roles import PlanningChoiceFn
from digitaljulius.single_agent import _single_agent_run

log = get_logger(__name__)

# Secondary boss. When the primary boss (claude) fully exhausts its model
# chain, the orchestrator escalates here directly rather than walking down
# the rest of the routing list. Top of openai's chain = gpt-5.
PRIMARY_BOSS = "claude"
SECONDARY_BOSS = "openai"


# A confirm callback returns True to proceed, False to skip.
ConfirmFn = Callable[[str], bool]


@dataclass
class RunResult:
    classification: Classification
    plan_verdict: Verdict | None = None
    plan: Plan | None = None  # drafted plan (Phase 2 plan-then-execute)
    responses: list[AgentResponse] = field(default_factory=list)
    output_verdict: Verdict | None = None
    final_text: str = ""
    chosen_agent: str = ""
    chosen_model: str = ""
    consensus: ConsensusResult | None = None
    skipped_reason: str = ""


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------


def _pick_agents_for(tags: list[str], cfg: dict, max_n: int) -> list[str]:
    """Return the ordered agent list to try for these tags, skipping any
    agent that is uninstalled, unauthenticated, disabled, or fully exhausted."""
    routing = cfg.get("routing", {})
    seen: list[str] = []
    for tag in tags or ["default"]:
        for agent in routing.get(tag, routing.get("default", [])):
            if agent not in seen:
                seen.append(agent)
    for agent in routing.get("default", []):
        if agent not in seen:
            seen.append(agent)
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
            if best_available_model(cfg, agent):
                final.append(agent)
    return final


def _handoff_preamble(prev_agent: str, prev_model: str, reason: str,
                      attempts: list[tuple[str, str, str]]) -> str:
    """Build a context-handoff block injected into the next agent's prompt
    when we rotate mid-turn. Tells the new agent what was originally asked,
    who tried before it, and why each previous attempt was abandoned — so
    the next agent doesn't restart from scratch or ask the user to repeat.
    """
    lines = [
        "[CONTEXT HANDOFF — DigitalJulius rotated this work to you mid-turn.]",
        f"You are now the active worker (previously: {prev_agent}/{prev_model}).",
        f"Reason for handoff: {reason}",
    ]
    if attempts:
        lines.append("Prior attempts in this turn:")
        for ag, mdl, why in attempts:
            lines.append(f"  - {ag}/{mdl} → {why}")
    lines.append("")
    lines.append("Continue the original work below WITHOUT asking the user to "
                 "repeat themselves. They asked exactly this once; deliver it.")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _try_agents_in_order(
    prompt: str,
    agents: list[str],
    cfg: dict,
    cwd: Path | None,
    on_event: Reporter,
    tags: list[str] | None = None,
) -> AgentResponse | None:
    """Walk `agents` until one returns a non-quota response. Returns None if
    every agent was exhausted.

    Special case: when the PRIMARY_BOSS (claude) exhausts, we don't walk
    to the next agent in the original list — we promote the SECONDARY_BOSS
    (openai) to the very next slot. This is the "Claude credits dry up
    => jump straight to OpenAI top-tier" rule.

    Cross-agent context: every rotation injects a HANDOFF preamble naming
    the previous agent + reason, so the new agent picks up the user's
    original intent instead of restarting cold.
    """
    last_non_quota: AgentResponse | None = None
    queue = list(agents)
    seen: set[str] = set()
    attempts: list[tuple[str, str, str]] = []  # (agent, model, why_failed)
    last_failed_agent = ""
    last_failed_model = ""
    last_failure_reason = ""
    self_modify = "self_modify" in (tags or [])

    while queue:
        agent = queue.pop(0)
        if agent in seen:
            continue
        seen.add(agent)
        log.info("orchestrator.route try=%s remaining=%s", agent, queue)

        # Inject handoff context if this isn't the first attempt.
        agent_prompt = prompt
        if attempts:
            agent_prompt = _handoff_preamble(
                last_failed_agent, last_failed_model, last_failure_reason,
                attempts,
            ) + prompt

        resp = _single_agent_run(agent_prompt, agent, cfg, cwd, on_event)
        if resp.ok:
            log.info("orchestrator.route agent=%s succeeded handoffs=%d",
                     agent, len(attempts))
            return resp

        # Record this attempt for the next agent's handoff block.
        last_failed_agent = agent
        last_failed_model = resp.model or "?"
        if resp.stderr == "QUOTA_EXCEEDED":
            last_failure_reason = "credits/quota exhausted"
        else:
            last_failure_reason = (resp.stderr or "agent error")[:120]
        attempts.append((agent, last_failed_model, last_failure_reason))

        if resp.stderr == "QUOTA_EXCEEDED":
            if self_modify:
                msg = f"{agent} exhausted on a self-modification task; rerun later or top up."
                resp.stderr = msg
                on_event(StepEvent(kind="route_done", label=msg, agent=agent))
                return resp
            note = f"{agent} exhausted — rotating to next agent (with handoff context)"
            if agent == PRIMARY_BOSS and SECONDARY_BOSS not in seen:
                if SECONDARY_BOSS in queue:
                    queue.remove(SECONDARY_BOSS)
                queue.insert(0, SECONDARY_BOSS)
                note = (f"{PRIMARY_BOSS} credits exhausted — escalating to "
                        f"{SECONDARY_BOSS} top-tier (gpt-5) with full context handoff")
                log.warning("orchestrator.escalate primary=%s -> secondary=%s",
                            PRIMARY_BOSS, SECONDARY_BOSS)
                on_event(StepEvent(
                    kind="escalate_done",
                    label=note,
                    agent=SECONDARY_BOSS,
                ))
            else:
                on_event(StepEvent(kind="route_done", label=note))
            continue
        last_non_quota = resp
    log.info("orchestrator.route exhausted all=%s", list(seen))
    return last_non_quota


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------


PlanReviewFn = Callable[[Plan], "Plan | None"]


def run_prompt(
    prompt: str,
    cfg: dict,
    cwd: Path | None = None,
    confirm: ConfirmFn | None = None,
    on_event: Reporter | None = None,
    project_context: str = "",
    history_context: str = "",
    confirm_planning: PlanningChoiceFn | None = None,
    review_drafted_plan: PlanReviewFn | None = None,
    force_plan_first: bool = False,
) -> RunResult:
    """Top-level: classify → (optional) draft+approve plan → execute → review.

    `review_drafted_plan` is called with the drafted Plan; it should return
    an approved Plan (possibly edited) or None to abort. Triggered when:
      - tier is COMPLEX or CRITICAL, OR
      - cfg.general.always_plan_first is True, OR
      - force_plan_first is True (set by /plan slash command)
    """
    cwd = cwd or Path.cwd()
    on_event = on_event or silent

    # Phase 3: progress reporter snapshots cwd now so we can diff after the
    # worker agents finish editing files.
    progress = ProgressReporter(cwd=cwd, notify=lambda s: on_event(StepEvent(
        kind="progress_done", label=s,
    )))

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

    # Build the prompt the *agents* will see (classifier already ran on the
    # raw prompt). Layered: knowledge KB (auto-learned lessons) → project
    # context (PROJECT.md / shared-agent-context) → conversation history →
    # user prompt.
    prefix_blocks: list[str] = []
    if tier != Tier.SIMPLE:
        kb = context_for_prompt()
        if kb:
            prefix_blocks.append(kb)
    if project_context:
        prefix_blocks.append(project_context)
    if history_context:
        prefix_blocks.append(history_context)
    if prefix_blocks:
        enriched_prompt = "\n\n".join(prefix_blocks) + f"\n\n---\nUser prompt:\n{prompt}"
    else:
        enriched_prompt = prompt

    # Plan-then-execute (Phase 2): draft a plan first when the task is
    # non-trivial OR the user explicitly requested it. Top-tier-only via the
    # planning role; user reviews/approves via review_drafted_plan callback.
    plan_first = (
        force_plan_first
        or cfg.get("general", {}).get("always_plan_first", False)
        or tier in (Tier.COMPLEX, Tier.CRITICAL)
    )
    if plan_first and review_drafted_plan is not None:
        on_event(StepEvent(kind="plan_draft_start", label="drafting plan"))
        t_plan = time.time()
        drafted = draft_plan(prompt, cfg, cwd=cwd, confirm_planning=confirm_planning)
        on_event(StepEvent(
            kind="plan_draft_done",
            label=(
                f"plan drafted: {len(drafted.steps)} steps"
                if drafted is not None else
                "couldn't draft a plan, escalating to direct execution"
            ),
            duration_s=time.time() - t_plan,
        ))
        if drafted is None:
            log.warning("orchestrator.plan_draft_failed_direct_execution")
        else:
            approved = review_drafted_plan(drafted)
            if approved is None:
                result.skipped_reason = "plan rejected by user"
                log.info("orchestrator.plan_rejected")
                return result
            result.plan = approved
            # Inject the approved plan into the worker prompt so agents follow
            # it instead of re-deriving their own approach.
            enriched_prompt = plan_to_worker_prefix(approved) + "\n\n" + enriched_prompt

    # Apply the user-locked execution directive so every worker agent
    # executes the task instead of returning manual instructions.
    enriched_prompt = wrap_for_execution(enriched_prompt)

    # ---- SIMPLE and MODERATE: one agent at a time with full fallback ----
    if tier in (Tier.SIMPLE, Tier.MODERATE):
        agents = _pick_agents_for(tags, cfg, max_n=5)
        if not agents:
            result.skipped_reason = "no authenticated agent with quota available"
            return result

        resp = _try_agents_in_order(enriched_prompt, agents, cfg, cwd, on_event, tags)
        if resp is None:
            result.skipped_reason = "all agents exhausted for today"
            return result

        result.responses.append(resp)
        if not resp.ok:
            result.skipped_reason = resp.stderr or "agent failed"
            return result
        result.final_text = resp.text
        result.chosen_agent = resp.agent
        result.chosen_model = resp.model
        progress.harvest(resp.text)

        if tier == Tier.MODERATE and resp.ok and resp.text:
            approver_cfg = cfg.get("approver", {})
            on_event(StepEvent(
                kind="review_start", label="reviewing output",
                agent=approver_cfg.get("agent", "claude"),
                model=approver_cfg.get("model", "opus"),
            ))
            t1 = time.time()
            result.output_verdict = review_output(
                prompt, resp.agent, resp.text, cfg, cwd,
                confirm_planning=confirm_planning,
            )
            on_event(StepEvent(
                kind="review_done",
                label=f"output review: {'OK' if result.output_verdict.approved else 'BLOCK'}",
                duration_s=time.time() - t1,
                note=result.output_verdict.critique[:200] if result.output_verdict.critique else "",
            ))
        return result

    # ---- COMPLEX and CRITICAL: consensus path ----
    max_n = 2 if tier == Tier.COMPLEX else 3
    pick_max_n = 5 if tier == Tier.COMPLEX else max_n
    agents = _pick_agents_for(tags, cfg, max_n=pick_max_n)
    if not agents:
        result.skipped_reason = "no authenticated agents available"
        return result

    # COMPLEX/CRITICAL: route to openai + codex in parallel as primary
    # workers when both are available. They're the highest-tier non-Claude
    # options — fast (parallel) and high-quality. Claude joins as third
    # voice for consensus when CRITICAL.
    parallel_pair = [a for a in ("openai", "codex") if a in agents]
    if parallel_pair and tier in (Tier.COMPLEX, Tier.CRITICAL):
        # Move the parallel pair to the front before applying the tier cap,
        # so COMPLEX keeps both halves of the pair when both are available.
        rest = [a for a in agents if a not in parallel_pair]
        agents = (parallel_pair + rest)[:max_n]
        log.info("orchestrator.complex_parallel agents=%s", agents)
        on_event(StepEvent(
            kind="route_done",
            label=f"COMPLEX tier — fanning out to {parallel_pair} in parallel",
        ))

    twin = False
    if tier == Tier.CRITICAL:
        if cfg.get("general", {}).get("auto_spawn_twin"):
            twin = True
        elif confirm is not None:
            twin = confirm(
                "This task is CRITICAL. Spawn a parallel twin-instance consensus run for redundancy? (y/N): "
            )

    on_event(StepEvent(
        kind="route_done",
        label=f"routing to {agents}" + (" + twin" if twin else ""),
    ))

    approver_cfg = cfg.get("approver", {})
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
        result.plan_verdict = review_plan(
            prompt, plan_summary, cfg, cwd,
            confirm_planning=confirm_planning,
        )
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

    # If consensus came back empty (everyone hit quota), degrade gracefully
    # to single-agent fallback rather than returning nothing.
    if not any(r.ok for r in consensus.responses):
        on_event(StepEvent(
            kind="route_done",
            label="consensus empty — degrading to single-agent fallback",
        ))
        fallback = _try_agents_in_order(enriched_prompt, agents, cfg, cwd, on_event, tags)
        if fallback and fallback.ok:
            consensus.responses.append(fallback)

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
    final = synthesise(prompt, consensus, cfg, cwd=cwd, confirm_planning=confirm_planning)
    result.final_text = final
    result.chosen_agent = consensus.chosen_agent
    result.chosen_model = consensus.chosen_model
    progress.harvest(final)
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
            prompt, result.chosen_agent or "consensus", final, cfg, cwd,
            confirm_planning=confirm_planning,
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
            result.skipped_reason = "output blocked by approver"

    return result
