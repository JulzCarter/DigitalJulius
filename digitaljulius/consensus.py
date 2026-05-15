"""Multi-agent consensus.

Fan out a prompt to several agents in parallel, then ask the approver
to synthesise the best answer from their responses.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from digitaljulius.agents.base import AgentResponse
from digitaljulius.agents.registry import get_agent
from digitaljulius.budget import best_available_model, record_call
from digitaljulius.events import Reporter, StepEvent, silent
from digitaljulius.roles import PlanningChoiceFn, resilient_role_call


@dataclass
class ConsensusResult:
    responses: list[AgentResponse] = field(default_factory=list)
    synthesis: str = ""
    chosen_agent: str = ""
    chosen_model: str = ""


SYNTH_PROMPT = """You are the synthesiser for a multi-agent orchestrator. Three
coding agents (Claude Code, Gemini CLI, GitHub Models) independently answered
the same user prompt. Read all candidate answers and produce the single best
final answer.

Rules:
- Prefer correctness over verbosity.
- If candidates disagree on a fact, flag the disagreement briefly and pick the
  most defensible answer.
- Do NOT invent capabilities the candidates didn't mention.
- Keep the user's original tone (terse if they were terse).

User prompt:
---
{prompt}
---

{candidates}

Write the final answer now. Do not preface it with "Final answer:" — just
write the answer."""


def _call(
    agent_name: str,
    model: str,
    prompt: str,
    cwd: Path | None,
    on_event: Reporter,
) -> AgentResponse:
    import time
    adapter = get_agent(agent_name)
    on_event(StepEvent(
        kind="agent_start", label=f"{agent_name} generating",
        agent=agent_name, model=model,
    ))
    t0 = time.time()
    resp = adapter.run(prompt, model=model, yolo=True, cwd=cwd, timeout=300)
    record_call(agent_name, model)
    on_event(StepEvent(
        kind="agent_done" if resp.ok else "agent_fail",
        label=f"{agent_name} {'done' if resp.ok else 'failed'}",
        agent=agent_name, model=model, duration_s=time.time() - t0,
        note="" if resp.ok else (resp.stderr or "")[:200],
    ))
    return resp


def run_consensus(
    prompt: str,
    cfg: dict,
    agents: list[str],
    cwd: Path | None = None,
    on_event: Reporter | None = None,
) -> ConsensusResult:
    """Call each agent in parallel with its best available model."""
    on_event = on_event or silent
    result = ConsensusResult()
    plan: list[tuple[str, str]] = []
    for agent in agents:
        model = best_available_model(cfg, agent)
        if not model:
            continue
        adapter = get_agent(agent)
        if not (adapter.is_installed() and adapter.is_authenticated()):
            continue
        plan.append((agent, model))

    if not plan:
        return result

    with ThreadPoolExecutor(max_workers=len(plan)) as pool:
        futures = {
            pool.submit(_call, agent, model, prompt, cwd, on_event): (agent, model)
            for agent, model in plan
        }
        for fut in as_completed(futures):
            try:
                result.responses.append(fut.result())
            except Exception as e:
                agent, model = futures[fut]
                result.responses.append(
                    AgentResponse(
                        agent=agent, model=model, ok=False, text="", stderr=str(e)
                    )
                )
    return result


def synthesise(
    prompt: str, result: ConsensusResult, cfg: dict,
    cwd: Path | None = None,
    confirm_planning: PlanningChoiceFn | None = None,
) -> str:
    """Ask the approver agent to merge candidate answers into a single reply.
    Synthesis IS a planning role — top-tier only, prompts the user before
    any downgrade."""
    good = [r for r in result.responses if r.ok and r.text]
    if not good:
        return ""
    if len(good) == 1:
        # Nothing to synthesise — just return the single answer.
        result.chosen_agent = good[0].agent
        result.chosen_model = good[0].model
        result.synthesis = good[0].text
        return good[0].text

    blocks = []
    for r in good:
        blocks.append(
            f"--- Candidate from {r.agent} ({r.model}) ---\n{r.text[:6000]}\n"
        )
    synth_prompt = (
        SYNTH_PROMPT
        .replace("{prompt}", prompt)
        .replace("{candidates}", "\n".join(blocks))
    )

    # Prefer the explicit synthesizer role if configured, else fall back to
    # the approver role (same agent does plan-review and synthesis by default).
    synth_cfg = cfg.get("synthesizer") or {}
    if not synth_cfg.get("agent"):
        synth_cfg = cfg.get("approver", {"agent": "claude", "model": "opus"})

    resp = resilient_role_call(
        synth_cfg, synth_prompt, cfg, cwd=cwd, timeout=180,
        planning=True, confirm_planning=confirm_planning,
    )
    if resp is not None and resp.ok and resp.text:
        result.chosen_agent = f"{resp.agent}-synth"
        result.chosen_model = resp.model
        result.synthesis = resp.text
        return resp.text

    # Last-ditch fallback: pick the longest candidate so the user still
    # gets a usable answer when every synth agent is down.
    best = max(good, key=lambda r: len(r.text))
    result.chosen_agent = best.agent
    result.chosen_model = best.model
    result.synthesis = best.text
    return best.text
