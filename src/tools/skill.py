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

from src import config
from src.types import (
    AgentState,
    Message,
    MessageHistory,
    ToolDef,
    ToolResult,
    ToolUseContext,
)
from src.skills import get_skill, build_skill_content
from src.types import InvokedSkillInfo


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

async def _execute(
    inputs: dict,
    context: ToolUseContext,
) -> ToolResult:
    """Execute a skill by name.

    Inline mode: returns ToolResult with new_messages + context_modifier.
    Fork mode: launches sub-agent loop via context.on_event, returns ToolResult.
    """
    skill_name = inputs.get("skill", "").strip()
    if not skill_name:
        return ToolResult(data={"success": False, "error": "No skill name provided"})

    if skill_name.startswith("/"):
        skill_name = skill_name[1:]

    skill = get_skill(skill_name)
    if skill is None:
        return ToolResult(data={"success": False, "error": f"Unknown skill: {skill_name}"})

    # --- Fork mode ---
    if skill.is_fork:
        if context.depth >= config.MAX_AGENT_DEPTH:
            return ToolResult(data={
                "success": False,
                "error": (
                    f"Fork skill '{skill_name}' cannot be invoked: "
                    f"maximum agent depth ({config.MAX_AGENT_DEPTH}) reached "
                    f"(current depth: {context.depth})."
                ),
            })
        return await _execute_fork(skill, inputs, context)

    # --- Inline mode ---
    return _execute_inline(skill, inputs, context)


# ---------------------------------------------------------------------------
# Inline mode — inject skill content into main conversation
# ---------------------------------------------------------------------------

def _execute_inline(skill, inputs: dict, context: ToolUseContext) -> ToolResult:
    """Inline execution: return new_messages + optional context_modifier."""
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

    # --- Record invoked skill for post-compact re-injection ---
    context.invoked_skills[skill.name] = InvokedSkillInfo(
        skill_name=skill.name,
        skill_path=skill.base_dir,
        content=content,
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

async def _execute_fork(
    skill,
    inputs: dict,
    context: ToolUseContext,
) -> ToolResult:
    """Fork execution: run skill in an isolated sub-agent loop.

    Events are emitted via context.on_event callback.
    Returns the final ToolResult.
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

    from src.messages import build_skill_reminder, build_agent_reminder

    sub_history = MessageHistory(initial_messages)

    msg = sub_history.last_user_message()
    if msg is not None:
        attachments = []
        skill_rem = build_skill_reminder(
            sub_tool_names,
            use_sent_tracking=False,
            exclude_fork_skills=True,
        )
        if skill_rem:
            attachments.append(skill_rem)
        agent_rem = build_agent_reminder(sub_tool_names, use_sent_tracking=False)
        if agent_rem:
            attachments.append(agent_rem)
        if attachments:
            msg.attach(attachments)

    from src.utils.file_state_cache import FileStateCache
    sub_context = ToolUseContext(
        messages=sub_history,
        tools=sub_tool_names,
        depth=context.depth + 1,
        abort_signal=context.abort_signal,
        permissions=context.permissions.as_silent() if context.permissions else None,
        on_event=context.on_event,
        file_state_cache=FileStateCache(),
    )

    sub_state = AgentState(agent_id=f"fork:{skill.name}")
    label = f"fork:{skill.name}"
    on_event = context.on_event

    try:
        from src.agent_loop import run_agent_loop

        result_text = await run_agent_loop(
            system_prompt=sub_system_prompt,
            tool_use_context=sub_context,
            max_turns=config.MAX_TURNS,
            state=sub_state,
            label=label,
            on_event=on_event,
        )
    except Exception as e:
        return ToolResult(data={
            "success": False,
            "commandName": skill.name,
            "error": f"Fork execution failed: {e}",
        })

    return ToolResult(data={
        "success": True,
        "commandName": skill.name,
        "status": "forked",
        "agentId": sub_state.agent_id,
        "result": result_text,
    })


# ---------------------------------------------------------------------------
# map_result — what the LLM sees as the tool_result text
# ---------------------------------------------------------------------------

def _map_result(data: dict) -> str:
    """Convert executor data to LLM-readable text."""
    if not data.get("success"):
        return f"Error: {data.get('error', 'Unknown error')}"

    status = data.get("status")
    name = data.get("commandName", "unknown")

    if status == "forked":
        result = data.get("result", "")
        return f'Skill "{name}" completed (forked execution).\n\nResult:\n{result}'

    return f"Launching skill: {name}"


# ---------------------------------------------------------------------------
# ToolDef instance — registered in ALL_TOOLS
# ---------------------------------------------------------------------------

tool = ToolDef(
    schema=SKILL_SCHEMA,
    executor=_execute,
    map_result=_map_result,
)
