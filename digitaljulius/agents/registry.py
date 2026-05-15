from __future__ import annotations

from digitaljulius.agents.base import AgentAdapter
from digitaljulius.agents.claude import ClaudeAdapter
from digitaljulius.agents.codex import CodexAdapter
from digitaljulius.agents.gemini import GeminiAdapter
from digitaljulius.agents.github import GitHubModelsAdapter
from digitaljulius.agents.openai_adapter import OpenAIAdapter

# Order matters: dict iteration order is the cross-agent fallback order used
# by roles.resilient_role_call. Claude is the primary boss; OpenAI is the
# secondary boss (jumped to when Claude credits run out); Codex handles
# deep coding; Gemini and GitHub are general-purpose workers.
AGENTS: dict[str, AgentAdapter] = {
    "claude": ClaudeAdapter(),
    "openai": OpenAIAdapter(),
    "codex": CodexAdapter(),
    "gemini": GeminiAdapter(),
    "github": GitHubModelsAdapter(),
}


def get_agent(name: str) -> AgentAdapter:
    if name not in AGENTS:
        raise KeyError(f"unknown agent: {name!r}. valid: {list(AGENTS)}")
    return AGENTS[name]


def installed_agents() -> dict[str, AgentAdapter]:
    return {n: a for n, a in AGENTS.items() if a.is_installed()}


def authenticated_agents() -> dict[str, AgentAdapter]:
    return {n: a for n, a in installed_agents().items() if a.is_authenticated()}
