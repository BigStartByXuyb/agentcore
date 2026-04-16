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
from src.types import ToolDef, ToolResult, ToolUseContext, AgentState
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

def _execute(inputs: dict, context: ToolUseContext) -> ToolResult:
    """Execute a skill by name.

    Inline mode (is_fork=False):
      Returns ToolResult with new_messages containing the skill content
      wrapped in <skill-content> tags, plus a context_modifier that
      restricts available tools if allow_tools is set.

    Fork mode (is_fork=True):
      Launches an isolated sub-agent loop, waits for completion,
      returns the result text in ToolResult.data.
    """
    skill_name = inputs.get("skill", "").strip()
    if not skill_name:
        return ToolResult(data={"success": False, "error": "No skill name provided"})

    # Strip leading slash for compatibility ("/commit" → "commit")
    if skill_name.startswith("/"):
        skill_name = skill_name[1:]

    skill = get_skill(skill_name)
    if skill is None:
        return ToolResult(data={"success": False, "error": f"Unknown skill: {skill_name}"})

    # --- Fork mode ---
    # Sub-agents (depth > 0) cannot invoke fork skills — that would create
    # a nested agent loop.  Only the top-level agent may fork.
    if skill.is_fork:
        if context.depth > 0:
            return ToolResult(data={
                "success": False,
                "error": (
                    f"Fork skill '{skill_name}' cannot be invoked from a sub-agent. "
                    "Only inline skills are allowed at this depth."
                ),
            })
        return (yield from _execute_fork(skill, inputs, context))

    # --- Inline mode ---
    return _execute_inline(skill, inputs, context)


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

    # Build the user message that injects skill content into the conversation
    skill_message = {
        "role": "user",
        "content": (
            f"<skill-content name='{skill.name}'>\n"
            f"{content}\n"
            f"</skill-content>\n\n"
            "Please follow the skill instructions above."
        ),
    }

    # Build context_modifier if skill restricts tools
    context_modifier = None
    if skill.allowed_tools:
        # Snapshot the list — the closure must not see later mutations
        allowed = list(skill.allowed_tools)

        def _modifier(ctx: ToolUseContext) -> ToolUseContext:
            return ToolUseContext(
                messages=ctx.messages,
                tools=allowed,
                depth=ctx.depth,
                abort_signal=ctx.abort_signal,
            )

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

def _execute_fork(skill, inputs: dict, context: ToolUseContext) -> ToolResult:
    """Fork execution: run skill in an isolated sub-agent loop.

    Mirrors executeForkedSkill() in SkillTool.ts + prepareForkedCommandContext()
    in forkedAgent.ts:

      1. Check depth limit (prevent infinite recursion)
      2. Build skill content with $ARGUMENTS substitution
      3. Construct initial messages (single user message with <skill-content>)
      4. Build sub-agent system prompt
      5. Build tool schemas (respecting allowed_tools whitelist)
      6. Launch run_agent_loop() with isolated state
      7. Return result text in ToolResult.data

    The fork skill runs synchronously — _execute_fork blocks until the
    sub-agent completes.  No new_messages or context_modifier are returned;
    the parent conversation only sees the final result text.
    """
    # --- Depth check ---
    if context.depth >= config.MAX_AGENT_DEPTH:
        return ToolResult(
            data={
                "success": False,
                "commandName": skill.name,
                "error": (
                    f"Maximum agent nesting depth ({config.MAX_AGENT_DEPTH}) reached. "
                    f"Cannot fork skill '{skill.name}' at depth {context.depth}."
                ),
            }
        )

    # --- Build skill content ---
    content = build_skill_content(skill)
    args = inputs.get("args", "")
    if args:
        content = content.replace("$ARGUMENTS", args)

    # --- Initial messages for the sub-agent ---
    # Mirrors prepareForkedCommandContext() → promptMessages
    initial_messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"<skill-content name='{skill.name}'>\n"
                f"{content}\n"
                f"</skill-content>\n\n"
                "Execute the skill instructions above. "
                "When finished, provide your final result as plain text."
            ),
        }
    ]

    # --- Sub-agent system prompt ---
    sub_system_prompt = (
        f"You are a sub-agent executing the '{skill.name}' skill.\n"
        "Follow the skill instructions precisely.\n"
        "Do NOT spawn sub-agents or invoke additional skills; execute directly.\n"
        "When finished, provide a clear, concise result.\n"
    )

    # --- Tool names (respect allowed_tools whitelist) ---
    from src.tools import ALL_TOOLS  # local import to avoid circular

    if skill.allowed_tools:
        sub_tool_names = list(skill.allowed_tools)
    else:
        # No restriction — use all tools except Skill (prevent recursion via prompt)
        sub_tool_names = list(ALL_TOOLS.keys())

    # --- Sub-agent context ---
    sub_context = ToolUseContext(
        messages=initial_messages,
        tools=sub_tool_names,
        depth=context.depth + 1,
        abort_signal=context.abort_signal,
    )

    # --- Sub-agent state (isolated token tracking) ---
    sub_state = AgentState(agent_id=f"fork:{skill.name}")

    # --- Run the sub-agent loop ---
    label = f"fork:{skill.name}"

    try:
        from src.agent_loop import run_agent_loop  # local import to avoid circular

        gen = run_agent_loop(
            messages=initial_messages,
            system_prompt=sub_system_prompt,
            tool_use_context=sub_context,
            max_turns=config.MAX_TURNS,
            state=sub_state,
            label=label,
        )
        result_text = yield from gen  # bubble sub-agent events to parent
    except Exception as e:
        return ToolResult(
            data={
                "success": False,
                "commandName": skill.name,
                "error": f"Fork execution failed: {e}",
            }
        )

    return ToolResult(
        data={
            "success": True,
            "commandName": skill.name,
            "status": "forked",
            "agentId": sub_state.agent_id,
            "result": result_text,
        }
        # No new_messages — result is in data.result
        # No context_modifier — parent context unchanged
    )


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
