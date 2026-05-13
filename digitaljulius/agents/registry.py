from __future__ import annotations

from digitaljulius.agents.base import AgentAdapter
from digitaljulius.agents.claude import ClaudeAdapter
from digitaljulius.agents.gemini import GeminiAdapter
from digitaljulius.agents.qwen import QwenAdapter

AGENTS: dict[str, AgentAdapter] = {
    "claude": ClaudeAdapter(),
    "gemini": GeminiAdapter(),
    "qwen": QwenAdapter(),
}


def get_agent(name: str) -> AgentAdapter:
    if name not in AGENTS:
        raise KeyError(f"unknown agent: {name!r}. valid: {list(AGENTS)}")
    return AGENTS[name]


def installed_agents() -> dict[str, AgentAdapter]:
    return {n: a for n, a in AGENTS.items() if a.is_installed()}


def authenticated_agents() -> dict[str, AgentAdapter]:
    return {n: a for n, a in installed_agents().items() if a.is_authenticated()}
