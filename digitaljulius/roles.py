"""Resilient role calls — classifier, approver, synthesiser.

These three are "internal" LLM calls (not user-facing answers): the
orchestrator uses them to classify prompts, review outputs, and merge
multiple candidate answers. If the configured agent for a role is exhausted
or unauthenticated we still want the call to succeed by falling back to any
other authenticated agent — otherwise the whole pipeline goes dead the
moment Claude credits run out.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from digitaljulius.agents.base import AgentResponse
from digitaljulius.agents.registry import AGENTS, get_agent
from digitaljulius.budget import best_available_model, exhaust_model, record_call
from digitaljulius.core_directives import (
    downgrade_options,
    top_tier_planning_chain,
)
from digitaljulius.log import get_logger
from digitaljulius.providers import get_provider, list_providers

log = get_logger(__name__)

# Callback signature: orchestrator passes a function that, given a list of
# (agent, model) options, returns the user's chosen tuple or None to abort.
PlanningChoiceFn = Callable[[list[tuple[str, str]]], "tuple[str, str] | None"]


# Process-local cache of agents that have failed with a non-quota error in
# the current run (e.g. Gemini bailing on an untrusted folder, or a CLI that
# can't authenticate this session). We keep paying ~10s per call to retry
# these otherwise. Cleared when the process exits.
SESSION_SKIP: set[str] = set()


@dataclass(frozen=True)
class RoleHandle:
    name: str
    adapter: object
    budget_name: str

    def run(
        self,
        prompt: str,
        model: str,
        yolo: bool = True,
        cwd: Path | None = None,
        timeout: int = 120,
    ) -> AgentResponse:
        return self.adapter.run(prompt, model=model, yolo=yolo, cwd=cwd, timeout=timeout)


def resolve_role(role_cfg: dict) -> RoleHandle:
    """Resolve a role config through completion providers, then agents."""
    name = role_cfg.get("agent", "")
    if not name:
        raise KeyError("role config has no agent")
    try:
        adapter = get_provider(name)
    except KeyError:
        adapter = get_agent(name)
    budget_name = f"provider:{name}" if getattr(adapter, "kind", "") == "completion" else name
    return RoleHandle(name=name, adapter=adapter, budget_name=budget_name)


def mark_session_skip(agent_name: str, reason: str = "") -> None:
    SESSION_SKIP.add(agent_name)


def _is_soft_skip(resp: AgentResponse) -> bool:
    """Failure modes that mean 'this agent is broken in the current
    environment' — folder-trust, missing config, blocked policy. Worth
    skipping for the rest of the session so we don't pay 10s per prompt
    retrying it."""
    blob = ((resp.stderr or "") + " " + (resp.text or "")).lower()
    needles = (
        "not trusted", "untrusted", "trust the folder", "trust this folder",
        "trusted directory", "trusted folder", "skip-trust",
        "policy blocked", "permission denied", "not authenticated",
        "please login", "please log in",
    )
    return any(n in blob for n in needles)


def _looks_like_quota(resp: AgentResponse, adapter) -> bool:
    if hasattr(adapter, "is_quota_error") and adapter.is_quota_error(resp):
        return True
    blob = ((resp.stderr or "") + " " + (resp.text or "")).lower()
    needles = (
        "out of credits", "insufficient credits", "insufficient funds",
        "credit balance", "quota exceeded", "rate limit", "hit your limit",
        "usage limit", "402 ", "429 ",
    )
    return any(n in blob for n in needles)


def _try_agent_chain(
    agent_name: str,
    preferred_model: str,
    prompt: str,
    cwd: Path | None,
    timeout: int,
    cfg: dict,
) -> AgentResponse | None:
    """Try `preferred_model` then keep walking the agent's fallback chain on
    quota errors. Returns None when this agent has no usable model OR has
    already been marked as broken for this session (folder-trust etc)."""
    try:
        handle = resolve_role({"agent": agent_name})
    except KeyError:
        return None
    if handle.budget_name in SESSION_SKIP:
        return None
    adapter = handle.adapter
    if not (adapter.is_installed() and adapter.is_authenticated()):
        return None

    if getattr(adapter, "kind", "") == "completion":
        chain = getattr(adapter, "fallback_chain", []) or []
    else:
        chain = cfg.get("agents", {}).get(agent_name, {}).get("fallback_chain") or []
    models_to_try: list[str] = []
    if preferred_model:
        models_to_try.append(preferred_model)
    for m in chain:
        if m and m not in models_to_try:
            models_to_try.append(m)

    for model in models_to_try:
        # Skip already-exhausted models.
        if not best_available_model_for_specific(cfg, handle.budget_name, model):
            continue
        resp = handle.run(prompt, model=model, yolo=True, cwd=cwd, timeout=timeout)
        if resp.ok and resp.text:
            record_call(handle.budget_name, model)
            return resp
        if _looks_like_quota(resp, adapter):
            exhaust_model(handle.budget_name, model)
            continue
        if _is_soft_skip(resp):
            # E.g. Gemini "folder not trusted" — bail out for this whole
            # session so we stop paying 10s per prompt retrying it.
            mark_session_skip(handle.budget_name, "environment-not-supported")
            return resp
        # Non-quota failure — surface it; caller may try a different agent.
        return resp
    return None


def best_available_model_for_specific(cfg: dict, agent: str, model: str) -> bool:
    """Cheap predicate: is THIS specific model still under switch_pct today?"""
    from digitaljulius.budget import usage_pct
    switch = float(cfg["budget"]["switch_pct"])
    return usage_pct(cfg, agent, model) < switch


def _best_available_provider_model(cfg: dict, handle: RoleHandle) -> str | None:
    switch = float(cfg["budget"]["switch_pct"])
    from digitaljulius.budget import usage_pct
    for model in getattr(handle.adapter, "fallback_chain", []) or []:
        if usage_pct(cfg, handle.budget_name, model) < switch:
            return model
    return None


def resilient_role_call(
    role_cfg: dict,
    prompt: str,
    cfg: dict,
    cwd: Path | None = None,
    timeout: int = 120,
    *,
    planning: bool = False,
    confirm_planning: PlanningChoiceFn | None = None,
) -> AgentResponse | None:
    """Run an LLM call for a role (approver / classifier / synthesiser).

    Strategy (default, planning=False):
      1. Try the role's configured agent + model.
      2. If that agent is exhausted/unauth, walk every other authenticated
         agent in AGENTS, using its top_model.
      3. Return the first successful AgentResponse, or None if nothing works.

    Strategy (planning=True) — USER-LOCKED RULE:
      Planning roles (approver / synthesiser / plan-review) MUST use a
      TOP-TIER model. We try claude:opus → openai:gpt-5 → codex:gpt-5-codex
      in that order, but ONLY each boss agent's top_model — never their
      fallback chain. If every top-tier planner is exhausted, we call
      `confirm_planning(downgrade_options)` so the user explicitly picks
      a downgrade or aborts. We never silently fall back to haiku/mini.
    """
    if planning:
        return _planning_role_call(prompt, cfg, cwd, timeout, confirm_planning)

    primary = role_cfg.get("agent", "")
    primary_model = role_cfg.get("model", "")

    tried: set[str] = set()
    if primary:
        resp = _try_agent_chain(primary, primary_model, prompt, cwd, timeout, cfg)
        tried.add(primary)
        if resp and resp.ok and resp.text:
            return resp

    # Cross-provider fallback — any authenticated agent or completion provider
    # that has quota left. Built-in agent names remain authoritative.
    for agent_name, adapter in list_providers().items():
        if agent_name in tried:
            continue
        is_completion = getattr(adapter, "kind", "") == "completion"
        if is_completion:
            handle = RoleHandle(agent_name, adapter, f"provider:{agent_name}")
            model = _best_available_provider_model(cfg, handle)
        else:
            agent_cfg = cfg.get("agents", {}).get(agent_name, {})
            if not agent_cfg.get("enabled", True):
                continue
            model = best_available_model(cfg, agent_name)
        if not model:
            continue
        resp = _try_agent_chain(agent_name, model, prompt, cwd, timeout, cfg)
        if resp and resp.ok and resp.text:
            return resp

    return None


def _planning_role_call(
    prompt: str,
    cfg: dict,
    cwd: Path | None,
    timeout: int,
    confirm_planning: PlanningChoiceFn | None,
) -> AgentResponse | None:
    """Top-tier-only planner with user-confirmed downgrade.

    Walks PLANNING_BOSS_AGENTS' top_model only. On QUOTA_EXCEEDED for ALL
    top-tier planners, prompts the user (via confirm_planning) for an
    explicit downgrade choice; aborts if the user declines.
    """
    chain = top_tier_planning_chain(cfg)
    log.info("planner.start chain=%s", chain)
    for agent_name, model in chain:
        if agent_name in SESSION_SKIP:
            log.info("planner.skip agent=%s reason=session_skip", agent_name)
            continue
        try:
            adapter = get_agent(agent_name)
        except KeyError:
            continue
        if not (adapter.is_installed() and adapter.is_authenticated()):
            log.info("planner.skip agent=%s reason=unauth", agent_name)
            continue
        if not best_available_model_for_specific(cfg, agent_name, model):
            log.info("planner.skip agent=%s model=%s reason=budget_exhausted",
                     agent_name, model)
            continue
        log.info("planner.try agent=%s model=%s", agent_name, model)
        resp = adapter.run(prompt, model=model, yolo=True, cwd=cwd, timeout=timeout)
        if resp.ok and resp.text:
            record_call(agent_name, model)
            log.info("planner.ok agent=%s model=%s dur=%.1fs",
                     agent_name, model, resp.duration_s)
            return resp
        if _looks_like_quota(resp, adapter):
            log.warning("planner.quota agent=%s model=%s — escalating",
                        agent_name, model)
            exhaust_model(agent_name, model)
            continue
        if _is_soft_skip(resp):
            mark_session_skip(agent_name, "environment-not-supported")
            continue
        log.error("planner.fail agent=%s model=%s err=%s",
                  agent_name, model, (resp.stderr or "")[:200])
        return resp

    # Every top-tier planner exhausted. NO silent downgrade — prompt user.
    options = downgrade_options(cfg)
    log.warning("planner.exhausted_top_tier offering_downgrades=%s", options)
    if not confirm_planning or not options:
        log.error("planner.aborted no_confirm_callback_or_options")
        return None

    try:
        choice = confirm_planning(options)
    except Exception as e:
        log.error("planner.confirm_planning_raised err=%s", e)
        return None
    if not choice:
        log.warning("planner.user_aborted")
        return None

    agent_name, model = choice
    log.warning("planner.downgrade_approved agent=%s model=%s", agent_name, model)
    try:
        adapter = get_agent(agent_name)
    except KeyError:
        return None
    resp = adapter.run(prompt, model=model, yolo=True, cwd=cwd, timeout=timeout)
    if resp.ok and resp.text:
        record_call(agent_name, model)
    return resp
