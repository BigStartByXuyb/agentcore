"""Skill tool — invokes a skill by name within the main conversation.

Corresponds to Claude Code's src/tools/SkillTool/SkillTool.ts.

This is a single ToolDef registered in ALL_TOOLS.  The LLM calls it with
{"skill": "my-skill"} and it:

  Inline mode (is_fork=False):
    1. Looks up the SkillInfo
    2. Builds the skill content (body + ${SKILL_DIR} substitution)
    3. Returns a ToolResult with:
       - data: {success, commandName, status: "inline"}
       - new_messages: [user message with <skill-content>]
       - context_modifier: optional tool whitelist restriction

  Fork mode (is_fork=True):
    1. Looks up the SkillInfo
    2. Builds the skill content
    3. Launches an isolated sub-agent loop (run_agent_loop)
    4. Returns a ToolResult with:
       - data: {success, commandName, status: "forked", result}
       - no new_messages, no context_modifier
"""

from __future__ import annotations

from typing import AsyncIterator

from src import config
from src.types import (
    AgentState,
    AsyncGenWithResult,
    Message,
    MessageHistory,
    ToolDef,
    ToolResult,
    ToolUseContext,
)
from src.skills import get_skill, build_skill_content


# ---------------------------------------------------------------------------
# Schema — what the LLM sees
# ---------------------------------------------------------------------------

SKILL_SCHEMA = {
    "name": "Skill",
    "description": (
        "Execute a skill within the main conversation.\n\n"
        "When users ask you to perform tasks, check if any of the "
        "available skills match.  Skills provide specialized capabilities "
        "and domain knowledge.\n\n"
        "How to invoke:\n"
        '- skill: "pdf" — invoke the pdf skill\n'
        '- skill: "commit", args: "-m \'Fix bug\'" — invoke with arguments\n\n'
        "Important:\n"
        "- Available skills are listed in system-reminder messages\n"
        "- When a skill matches, invoke it BEFORE generating other responses\n"
        "- Do not invoke a skill that is already running\n"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": 'The skill name. E.g., "commit", "review-pr"',
            },
            "args": {
                "type": "string",
                "description": "Optional arguments for the skill",
            },
        },
        "required": ["skill"],
    },
}


# ---------------------------------------------------------------------------
# Executor — dispatches to inline or fork mode
# ---------------------------------------------------------------------------

def _execute(
    inputs: dict,
    context: ToolUseContext,
) -> AsyncGenWithResult:
    """Execute a skill by name.

    Inline mode (is_fork=False):
      Returns an AsyncGenWithResult whose .result is a ToolResult with
      new_messages containing the skill content wrapped in <skill-content>
      tags, plus a context_modifier that restricts tools if allow_tools
      is set.  No events are yielded.

    Fork mode (is_fork=True):
      Returns an AsyncGenWithResult that yields sub-agent events as they
      happen and, on completion, sets .result to a ToolResult containing
      the final text.
    """
    skill_name = inputs.get("skill", "").strip()
    if not skill_name:
        return AsyncGenWithResult.of_value(
            ToolResult(data={"success": False, "error": "No skill name provided"})
        )

    # Strip leading slash for compatibility ("/commit" → "commit")
    if skill_name.startswith("/"):
        skill_name = skill_name[1:]

    skill = get_skill(skill_name)
    if skill is None:
        return AsyncGenWithResult.of_value(
            ToolResult(data={"success": False, "error": f"Unknown skill: {skill_name}"})
        )

    # --- Fork mode ---
    # Fork skills launch a sub-agent loop, which increases depth.
    # Block only when we've already hit the maximum nesting depth.
    if skill.is_fork:
        if context.depth >= config.MAX_AGENT_DEPTH:
            return AsyncGenWithResult.of_value(ToolResult(data={
                "success": False,
                "error": (
                    f"Fork skill '{skill_name}' cannot be invoked: "
                    f"maximum agent depth ({config.MAX_AGENT_DEPTH}) reached "
                    f"(current depth: {context.depth})."
                ),
            }))
        return _execute_fork(skill, inputs, context)

    # --- Inline mode ---
    return AsyncGenWithResult.of_value(_execute_inline(skill, inputs, context))


# ---------------------------------------------------------------------------
# Inline mode — inject skill content into main conversation
# ---------------------------------------------------------------------------

def _execute_inline(skill, inputs: dict, context: ToolUseContext) -> ToolResult:
    """Inline execution: return new_messages + optional context_modifier.

    Mirrors SkillTool.ts call() path for context != 'fork'.
    """
    content = build_skill_content(skill)
    args = inputs.get("args", "")
    if args:
        content = content.replace("$ARGUMENTS", args)

    skill_message = Message(
        role="user",
        content=(
            f"<skill-content name='{skill.name}'>\n"
            f"{content}\n"
            f"</skill-content>\n\n"
            "Please follow the skill instructions above."
        ),
        msg_type="meta",
    )

    context_modifier = None
    if skill.allowed_tools:
        allowed = list(skill.allowed_tools)

        def _modifier(ctx: ToolUseContext) -> ToolUseContext:
            from dataclasses import replace
            return replace(ctx, tools=allowed)

        context_modifier = _modifier

    return ToolResult(
        data={
            "success": True,
            "commandName": skill.name,
            "status": "inline",
        },
        new_messages=[skill_message],
        context_modifier=context_modifier,
    )


# ---------------------------------------------------------------------------
# Fork mode — launch an isolated sub-agent loop
# ---------------------------------------------------------------------------

def _execute_fork(
    skill,
    inputs: dict,
    context: ToolUseContext,
) -> AsyncGenWithResult:
    """Fork execution: run skill in an isolated sub-agent loop.

    Returns an AsyncGenWithResult that:
      - yields each AgentEvent from the sub-agent loop (bubbling up)
      - on completion, sets .result to the final ToolResult

    Mirrors executeForkedSkill() in SkillTool.ts + prepareForkedCommandContext()
    in forkedAgent.ts.
    """
    content = build_skill_content(skill)
    args = inputs.get("args", "")
    if args:
        content = content.replace("$ARGUMENTS", args)

    initial_messages: list[Message] = [
        Message(
            role="user",
            content=(
                f"<skill-content name='{skill.name}'>\n"
                f"{content}\n"
                f"</skill-content>\n\n"
                "Execute the skill instructions above. "
                "When finished, provide your final result as plain text."
            ),
            msg_type="meta",
        )
    ]

    sub_system_prompt = (
        f"You are a sub-agent executing the '{skill.name}' skill.\n"
        "Follow the skill instructions precisely.\n"
        "Do NOT spawn sub-agents or invoke additional skills; execute directly.\n"
        "When finished, provide a clear, concise result.\n"
    )

    if skill.allowed_tools:
        sub_tool_names = list(skill.allowed_tools)
    else:
        from src.tools import registry as tool_registry
        sub_tool_names = tool_registry.list_names()

    from src.messages import build_metadata_reminders

    sub_history = MessageHistory(initial_messages)

    msg = sub_history.last_user_message()
    if msg is not None:
        msg.attach(build_metadata_reminders(
            sub_tool_names,
            use_sent_tracking=False,
            exclude_fork_skills=True,
        ))

    sub_context = ToolUseContext(
        messages=sub_history,
        tools=sub_tool_names,
        depth=context.depth + 1,
        abort_signal=context.abort_signal,
        permissions=context.permissions.as_silent() if context.permissions else None,
    )

    sub_state = AgentState(agent_id=f"fork:{skill.name}")
    label = f"fork:{skill.name}"

    async def _impl(run: AsyncGenWithResult) -> AsyncIterator:
        try:
            from src.agent_loop import run_agent_loop  # local import to avoid circular

            gen = run_agent_loop(
                system_prompt=sub_system_prompt,
                tool_use_context=sub_context,
                max_turns=config.MAX_TURNS,
                state=sub_state,
                label=label,
            )
            async for ev in gen.events():
                yield ev
            result_text = gen.result
        except Exception as e:
            run.set_result(ToolResult(data={
                "success": False,
                "commandName": skill.name,
                "error": f"Fork execution failed: {e}",
            }))
            return

        run.set_result(ToolResult(data={
            "success": True,
            "commandName": skill.name,
            "status": "forked",
            "agentId": sub_state.agent_id,
            "result": result_text,
        }))

    return AsyncGenWithResult(_impl)


# ---------------------------------------------------------------------------
# map_result — what the LLM sees as the tool_result text
# ---------------------------------------------------------------------------

def _map_result(data: dict) -> str:
    """Convert executor data to LLM-readable text.

    Mirrors mapToolResultToToolResultBlockParam() in SkillTool.ts.
    For inline: "Launching skill: <name>"
    For forked: "Skill '<name>' completed (forked).\n\nResult:\n..."
    For errors: the error message
    """
    if not data.get("success"):
        return f"Error: {data.get('error', 'Unknown error')}"

    status = data.get("status")
    name = data.get("commandName", "unknown")

    if status == "forked":
        result = data.get("result", "")
        return f'Skill "{name}" completed (forked execution).\n\nResult:\n{result}'

    # inline
    return f"Launching skill: {name}"


# ---------------------------------------------------------------------------
# ToolDef instance — registered in ALL_TOOLS
# ---------------------------------------------------------------------------

tool = ToolDef(
    schema=SKILL_SCHEMA,
    executor=_execute,
    map_result=_map_result,
)
