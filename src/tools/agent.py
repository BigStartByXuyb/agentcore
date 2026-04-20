"""Agent tool — launches a sub-agent to handle complex tasks.

Corresponds to Claude Code's src/tools/AgentTool/AgentTool.tsx.

This is a single ToolDef registered in ALL_TOOLS.  The LLM calls it with
{"prompt": "...", "description": "..."} and it:

  1. Resolves the agent type (defaults to general-purpose)
  2. Checks depth limit
  3. Launches an isolated sub-agent loop via run_agent_loop()
  4. Returns the sub-agent's final output in ToolResult.data
"""

from __future__ import annotations

from typing import AsyncIterator

from src import config
from src.types import (
    AgentState,
    AsyncGenWithResult,
    Message,
    ToolDef,
    ToolResult,
    ToolUseContext,
)
from src.agents import get_agent, list_agents, AgentDefinition


# ---------------------------------------------------------------------------
# Default (general-purpose) agent — used when no agent_type specified
# ---------------------------------------------------------------------------

_DEFAULT_AGENT = AgentDefinition(
    name="general-purpose",
    description="General-purpose agent for complex, multi-step tasks.",
    system_prompt=(
        "You are a general-purpose sub-agent. Execute the task described "
        "in the user's prompt using the tools available to you.\n\n"
        "Guidelines:\n"
        "- Be thorough and complete the task fully\n"
        "- You may use available skills (inline mode) if they help your task\n"
        "- You may spawn sub-agents if the task benefits from delegation\n"
        "- When finished, provide a clear summary of what you did\n"
    ),
    max_turns=20,
    allowed_tools=None,  # all tools except agent itself
)


# ---------------------------------------------------------------------------
# Schema — what the LLM sees
# ---------------------------------------------------------------------------

def _build_agent_description() -> str:
    """Build the tool description. Agent listing is injected via system-reminder."""
    return (
        "Launch a sub-agent to handle complex, multi-step tasks. "
        "Each agent type has specific capabilities and tools available to it.\n\n"
        "Available agent types are listed in the <system-reminder> messages. "
        "Specify agent_type to select one (defaults to general-purpose).\n\n"
        "Usage notes:\n"
        "- Include a short description summarizing what the agent will do\n"
        "- The agent runs to completion and returns its final output"
    )


AGENT_SCHEMA = {
    "name": "agent",
    "description": _build_agent_description(),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "A short (3-5 word) description of the task",
            },
            "prompt": {
                "type": "string",
                "description": "The task for the agent to perform",
            },
            "agent_type": {
                "type": "string",
                "description": (
                    "The type of agent to use (e.g. 'Explore'). "
                    "Defaults to general-purpose if omitted."
                ),
            },
        },
        "required": ["description", "prompt"],
    },
}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

def _resolve_tools(agent_def: AgentDefinition) -> list[str]:
    """Build tool name list for the sub-agent.

    Resolution order (mirrors Claude Code's tool resolution):
      1. Start with allowed_tools whitelist (or all tools if None)
      2. Subtract disallowed_tools blacklist

    The 'agent' tool is NOT automatically excluded — sub-agents can spawn
    further sub-agents as long as depth < MAX_AGENT_DEPTH. Agent definitions
    can explicitly add 'agent' to disallowed_tools if they are leaf agents
    that should not delegate.
    """
    from src.tools import ALL_TOOLS  # local import to avoid circular

    if agent_def.allowed_tools is not None:
        # Whitelist — intersect with ALL_TOOLS to discard unknown names
        whitelist = set(agent_def.allowed_tools)
        tools = [name for name in ALL_TOOLS if name in whitelist]
    else:
        # No whitelist — all tools available
        tools = list(ALL_TOOLS.keys())

    # Apply disallowed_tools blacklist
    if agent_def.disallowed_tools:
        blacklist = set(agent_def.disallowed_tools)
        tools = [t for t in tools if t not in blacklist]

    return tools


def _execute(inputs: dict, context: ToolUseContext) -> AsyncGenWithResult:
    """Launch a sub-agent to execute the given prompt."""
    prompt = inputs.get("prompt", "").strip()
    description = inputs.get("description", "").strip()
    agent_type = inputs.get("agent_type", "").strip()

    if not prompt:
        return AsyncGenWithResult.of_value(
            ToolResult(data={"success": False, "error": "No prompt provided"})
        )

    # --- Resolve agent definition ---
    if agent_type:
        agent_def = get_agent(agent_type)
        if agent_def is None:
            available = ", ".join(a.name for a in list_agents())
            return AsyncGenWithResult.of_value(ToolResult(data={
                "success": False,
                "error": (
                    f"Unknown agent type: '{agent_type}'. "
                    f"Available: {available}, general-purpose"
                ),
            }))
    else:
        agent_def = _DEFAULT_AGENT

    # --- Depth check ---
    if context.depth >= config.MAX_AGENT_DEPTH:
        return AsyncGenWithResult.of_value(ToolResult(data={
            "success": False,
            "agent_name": agent_def.name,
            "error": (
                f"Maximum agent nesting depth ({config.MAX_AGENT_DEPTH}) reached. "
                f"Cannot launch agent '{agent_def.name}' at depth {context.depth}."
            ),
        }))

    # --- Build tool name list ---
    sub_tool_names = _resolve_tools(agent_def)

    # --- Build initial messages ---
    initial_messages: list[Message] = [
        Message(role="user", content=prompt, msg_type="human")
    ]

    from src.messages import build_metadata_reminders

    initial_messages.extend(build_metadata_reminders(
        sub_tool_names,
        use_sent_tracking=False,
        skill_filter=agent_def.skills,
        exclude_fork_skills=True,
    ))

    # --- Sub-agent context ---
    sub_context = ToolUseContext(
        messages=initial_messages,
        tools=sub_tool_names,
        depth=context.depth + 1,
        abort_signal=context.abort_signal,
    )

    # --- Sub-agent state ---
    sub_state = AgentState(agent_id=f"agent:{agent_def.name}")
    label = f"agent:{agent_def.name}"

    async def _impl(run: AsyncGenWithResult) -> AsyncIterator:
        try:
            from src.agent_loop import run_agent_loop  # local import to avoid circular

            gen = run_agent_loop(
                messages=initial_messages,
                system_prompt=agent_def.system_prompt,
                tool_use_context=sub_context,
                max_turns=agent_def.max_turns,
                state=sub_state,
                label=label,
            )
            async for ev in gen.events():
                yield ev
            result_text = gen.result
        except Exception as e:
            run.set_result(ToolResult(data={
                "success": False,
                "agent_name": agent_def.name,
                "error": f"Agent execution failed: {e}",
            }))
            return

        run.set_result(ToolResult(data={
            "success": True,
            "agent_name": agent_def.name,
            "result": result_text,
        }))

    return AsyncGenWithResult(_impl)


# ---------------------------------------------------------------------------
# map_result — what the LLM sees as the tool_result text
# ---------------------------------------------------------------------------

def _map_result(data: dict) -> str:
    """Convert executor data to LLM-readable text."""
    if not data.get("success"):
        return f"Error: {data.get('error', 'Unknown error')}"

    name = data.get("agent_name", "unknown")
    result = data.get("result", "")
    return f'Agent "{name}" completed.\n\nResult:\n{result}'


# ---------------------------------------------------------------------------
# ToolDef instance
# ---------------------------------------------------------------------------

tool = ToolDef(
    schema=AGENT_SCHEMA,
    executor=_execute,
    map_result=_map_result,
    is_read_only=lambda _: False,
)
