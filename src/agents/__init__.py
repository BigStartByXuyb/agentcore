"""Agent definitions — built-in + file-based discovery.

Design overview:
  - Built-in agents are registered at import time (Explore, etc.)
  - Custom agents are discovered from agents/ directories (AGENT.md files)
  - Custom agents with the same name override built-in ones
  - get_agents() / get_agent() provide cached access (same pattern as skills)

Agent definition file format (agents/<name>/AGENT.md):

    ---
    description: When to use this agent
    max_turns: 12
    allowed_tools: bash, read_file, grep
    disallowed_tools: Skill
    skills: commit, review-pr
    ---

    You are a specialized agent...
    (body = system_prompt)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src.frontmatter import parse_frontmatter
from src.core.types import Attachment


# ---------------------------------------------------------------------------
# AgentDefinition
# ---------------------------------------------------------------------------

@dataclass
class AgentDefinition:
    """A sub-agent type — built-in or loaded from AGENT.md.

    Attributes:
        name:             Unique identifier (e.g. "Explore")
        description:      When-to-use text shown to the LLM
        system_prompt:    System prompt for the sub-agent loop
        max_turns:        Maximum LLM turns before forced stop
        allowed_tools:    Whitelist — None means all tools (agent excluded)
        disallowed_tools: Blacklist — applied after allowed_tools
        skills:           Skill instance whitelist — None means all skills
                          (if set, only these skill names are injected in
                          the <system-reminder> listing; if None, the full
                          skill listing is injected)
    """

    name: str
    description: str
    system_prompt: str
    max_turns: int = 12
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    skills: list[str] | None = None


# ---------------------------------------------------------------------------
# Built-in agents
# ---------------------------------------------------------------------------

from src.agents.explore import explore_agent  # noqa: E402
from src.agents.plan import plan_agent  # noqa: E402
from src.agents.general_purpose import general_purpose_agent  # noqa: E402
from src.agents.verification import verification_agent  # noqa: E402

_BUILTIN_AGENTS: dict[str, AgentDefinition] = {
    explore_agent.name.lower(): explore_agent,
    plan_agent.name.lower(): plan_agent,
    general_purpose_agent.name.lower(): general_purpose_agent,
    verification_agent.name.lower(): verification_agent,
}


# ---------------------------------------------------------------------------
# File-based agent discovery
# ---------------------------------------------------------------------------

def _parse_allowed_tools(value: Any) -> list[str] | None:
    """Parse allowed_tools / allowed-tools from frontmatter.

    Returns None if not specified (= no restriction).
    """
    if value is None:
        return None
    if isinstance(value, list):
        result = [str(t).strip() for t in value if str(t).strip()]
        return result if result else None
    if isinstance(value, str):
        result = [t.strip() for t in value.split(",") if t.strip()]
        return result if result else None
    return None


def _parse_disallowed_tools(value: Any) -> list[str] | None:
    """Parse disallowed_tools / disallowed-tools from frontmatter."""
    return _parse_allowed_tools(value)  # same parsing logic


def _parse_skills(value: Any) -> list[str] | None:
    """Parse skills from frontmatter.

    Returns None if not specified (= no restriction, all skills visible).
    Accepts comma-separated string or YAML list.
    """
    return _parse_allowed_tools(value)  # same parsing logic


def _load_agent_from_dir(agent_dir: str) -> AgentDefinition | None:
    """Load a single agent from its directory (agent_dir/AGENT.md).

    Returns None if AGENT.md is missing or unparseable.
    """
    agent_file = os.path.join(agent_dir, "AGENT.md")
    if not os.path.isfile(agent_file):
        return None

    try:
        with open(agent_file, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None

    fm, body = parse_frontmatter(raw)
    name = os.path.basename(agent_dir)

    description = str(fm.get("description", "") or "")
    if not description:
        description = f"Agent: {name}"

    max_turns = int(fm.get("max_turns", 12))
    allowed_tools = _parse_allowed_tools(
        fm.get("allowed_tools") or fm.get("allowed-tools")
    )
    disallowed_tools = _parse_disallowed_tools(
        fm.get("disallowed_tools") or fm.get("disallowed-tools")
    )
    skills = _parse_skills(fm.get("skills"))

    system_prompt = body.strip()
    if not system_prompt:
        system_prompt = f"You are the '{name}' agent. Follow the user's instructions."

    return AgentDefinition(
        name=name,
        description=description,
        system_prompt=system_prompt,
        max_turns=max_turns,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        skills=skills,
    )


def discover_agents(agents_dirs: list[str] | None = None) -> dict[str, AgentDefinition]:
    """Scan agent directories and return {lowercase_name: AgentDefinition}.

    Default search paths (if agents_dirs is None):
      1. <project>/agents/
      2. <project>/.claude/agents/

    Built-in agents are included first; file-based agents override them.
    Later directories override earlier ones.
    """
    # Start with built-in agents
    agents: dict[str, AgentDefinition] = dict(_BUILTIN_AGENTS)

    if agents_dirs is None:
        from src.core.config import get_agent_dirs
        agents_dirs = get_agent_dirs()

    for base_dir in agents_dirs:
        if not os.path.isdir(base_dir):
            continue

        try:
            entries = os.listdir(base_dir)
        except OSError:
            continue

        for entry in sorted(entries):
            entry_path = os.path.join(base_dir, entry)
            if not os.path.isdir(entry_path):
                continue

            agent = _load_agent_from_dir(entry_path)
            if agent is not None:
                agents[agent.name.lower()] = agent

    return agents


# ---------------------------------------------------------------------------
# Cached access (same pattern as skills)
# ---------------------------------------------------------------------------

_cached_agents: dict[str, AgentDefinition] | None = None


def get_agents(force_reload: bool = False) -> dict[str, AgentDefinition]:
    """Return the agent registry, loading on first call."""
    global _cached_agents
    if _cached_agents is None or force_reload:
        _cached_agents = discover_agents()
    return _cached_agents


def get_agent(name: str) -> AgentDefinition | None:
    """Look up a single agent by name (case-insensitive)."""
    return get_agents().get(name.lower())


def list_agents() -> list[AgentDefinition]:
    """Return all registered agent definitions."""
    return list(get_agents().values())


# ---------------------------------------------------------------------------
# Agent listing / reminder injection (mirrors skills' build_skill_attachment)
# ---------------------------------------------------------------------------

_MAX_LISTING_DESC_CHARS = 200
_sent_agent_names: set[str] = set()


def _format_tools_suffix(agent: AgentDefinition) -> str:
    """Build the (Tools: ...) suffix for an agent listing line."""
    if agent.allowed_tools is not None:
        tools = [t for t in agent.allowed_tools]
        if agent.disallowed_tools:
            tools = [t for t in tools if t not in agent.disallowed_tools]
        return f"(Tools: {', '.join(tools)})"
    if agent.disallowed_tools:
        excluded = ", ".join(agent.disallowed_tools)
        return f"(Tools: All tools except {excluded})"
    return "(Tools: *)"


def format_agent_listing(agents: dict[str, AgentDefinition]) -> str:
    """Build the text that tells the LLM which agents are available."""
    if not agents:
        return ""

    lines = [
        "The following agent types are available for use with the agent tool.\n"
        "Specify the agent_type parameter to select one (defaults to general-purpose):\n",
    ]
    for agent in agents.values():
        desc = agent.description
        if len(desc) > _MAX_LISTING_DESC_CHARS:
            desc = desc[: _MAX_LISTING_DESC_CHARS - 1] + "\u2026"
        tools_suffix = _format_tools_suffix(agent)
        lines.append(f"- {agent.name}: {desc} {tools_suffix}")
    return "\n".join(lines)


def build_agent_attachment(*, force: bool = False) -> Attachment | None:
    """Build a <system-reminder> user message listing available agents.

    Called by build_agent_reminder() in messages.py — not intended for direct external use.

    Parameters:
        force: If True, reset sent tracking and return all agents (used after compact).
    """
    global _sent_agent_names

    if force:
        _sent_agent_names = set()

    all_agents = get_agents()
    if not all_agents:
        return None

    new_agents = {
        name: defn
        for name, defn in all_agents.items()
        if name not in _sent_agent_names
    }

    if not new_agents:
        return None

    listing = format_agent_listing(new_agents)
    if not listing:
        return None

    _sent_agent_names.update(new_agents.keys())
    return Attachment(
        type="system_reminder",
        content=f"<system-reminder>\n{listing}\n</system-reminder>"
    )


def reset_sent_agents() -> None:
    """Clear the sent-agent tracker (e.g. on /clear or session reset)."""
    global _sent_agent_names
    _sent_agent_names = set()
