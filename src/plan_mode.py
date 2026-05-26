"""Plan Mode — state management and helpers.

Corresponds to Claude Code's plan mode: restricts tools to read-only + plan
file writing, injects plan mode attachment, and provides post-compact recovery.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from src.core.types import Attachment, ToolDef, ToolResult


# Module-level slug cache: session_id → slug string.
# Same session reuses the same plan file. /clear calls clear_slug_cache().
_slug_cache: dict[str, str] = {}


def clear_slug_cache(session_id: str) -> None:
    """Remove cached slug for a session (called on /clear)."""
    _slug_cache.pop(session_id, None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enter_plan_mode(session_id: str = "main") -> str:
    """Return a plan file path for this session.

    The slug is cached per session_id so the same session reuses the
    same plan file. Call clear_slug_cache() on /clear to reset.
    """
    plan_dir = os.path.join(str(Path.home()), ".my-agent", "plans")
    os.makedirs(plan_dir, exist_ok=True)

    if session_id in _slug_cache:
        slug = _slug_cache[session_id]
    else:
        slug = uuid.uuid4().hex[:12]
        _slug_cache[session_id] = slug

    return os.path.join(plan_dir, f"plan-{slug}.md")


def build_plan_mode_attachment(plan_file_path: str) -> Attachment:
    """Build the plan mode constraint attachment injected into user messages."""
    content = (
        "<system-reminder>\n"
        "Plan mode is active. The user indicated that they do not want you to execute yet "
        "-- you MUST NOT make any edits (with the exception of the plan file mentioned below), "
        "run any non-readonly tools (including changing configs or making commits), or otherwise "
        "make any changes to the system. This supercedes any other instructions you have received.\n\n"

        "## Plan File Info\n"
        f"Write your plan to: {plan_file_path}\n"
        "You should build your plan incrementally by writing to or editing this file. "
        "NOTE that this is the only file you are allowed to edit - other than this you are "
        "only allowed to take READ-ONLY actions.\n\n"

        "## Plan Workflow\n\n"

        "### Phase 1: Initial Understanding\n"
        "Goal: Gain a comprehensive understanding of the user's request by reading through "
        "code and asking them questions. Critical: In this phase you should only use the "
        "Explore subagent type.\n\n"
        "1. Focus on understanding the user's request and the code associated with their request. "
        "Actively search for existing functions, utilities, and patterns that can be reused "
        "— avoid proposing new code when suitable implementations already exist.\n\n"
        "2. **Launch up to 3 Explore agents IN PARALLEL** (single message, multiple tool calls) "
        "to efficiently explore the codebase.\n"
        "   - Use 1 agent when the task is isolated to known files, the user provided specific "
        "file paths, or you're making a small targeted change.\n"
        "   - Use multiple agents when: the scope is uncertain, multiple areas of the codebase "
        "are involved, or you need to understand existing patterns before planning.\n"
        "   - Quality over quantity - 3 agents maximum, but you should try to use the minimum "
        "number of agents necessary (usually just 1)\n"
        "   - If using multiple agents: Provide each agent with a specific search focus or area "
        "to explore.\n\n"

        "### Phase 2: Design\n"
        "Goal: Design an implementation approach.\n\n"
        "Launch a Plan agent (using the agent tool with agent_type='Plan') to design the "
        "implementation based on the user's intent and your exploration results from Phase 1.\n\n"
        "Guidelines:\n"
        "- **Default**: Launch a Plan agent for most tasks - it provides an independent "
        "architectural perspective and helps validate your understanding\n"
        "- **Skip the Plan agent**: Only for truly trivial tasks (typo fixes, single-line changes, "
        "simple renames)\n"
        "- In the agent prompt:\n"
        "  - Provide comprehensive background context from Phase 1 exploration including filenames "
        "and code path traces\n"
        "  - Describe requirements and constraints\n"
        "  - Request a detailed implementation plan\n\n"

        "### Phase 3: Review\n"
        "Goal: Review the plan and ensure alignment with the user's intentions.\n"
        "1. Read the critical files identified during exploration to deepen your understanding\n"
        "2. Ensure that the plan aligns with the user's original request\n"
        "3. Use AskUserQuestion to clarify any remaining questions with the user\n\n"

        "### Phase 4: Final Plan\n"
        "Goal: Write your final plan to the plan file (the only file you can edit).\n"
        "- Begin with a **Context** section: explain why this change is being made — the problem "
        "or need it addresses, what prompted it, and the intended outcome\n"
        "- Include only your recommended approach, not all alternatives\n"
        "- Ensure that the plan file is concise enough to scan quickly, but detailed enough to "
        "execute effectively\n"
        "- Include the paths of critical files to be modified\n"
        "- Reference existing functions and utilities you found that should be reused, with their "
        "file paths\n"
        "- Include a verification section describing how to test the changes end-to-end\n\n"

        "### Phase 5: Call ExitPlanMode\n"
        "At the very end of your turn, once you have asked the user questions and are happy with "
        "your final plan file - you should always call ExitPlanMode to indicate to the user that "
        "you are done planning.\n"
        "This is critical - your turn should only end with either using the AskUserQuestion tool "
        "OR calling ExitPlanMode. Do not stop unless it's for these 2 reasons.\n\n"
        "**Important:** Use AskUserQuestion ONLY to clarify requirements or choose between "
        "approaches. Use ExitPlanMode to request plan approval. Do NOT ask about plan approval "
        "in any other way - no text questions, no AskUserQuestion. Phrases like \"Is this plan "
        "okay?\", \"Should I proceed?\", \"How does this plan look?\" MUST use ExitPlanMode.\n\n"

        "NOTE: At any point in time through this workflow you should feel free to ask the user "
        "questions or clarifications using the AskUserQuestion tool. Don't make large assumptions "
        "about user intent. The goal is to present a well researched plan to the user, and tie "
        "any loose ends before implementation begins.\n"
        "</system-reminder>"
    )
    return Attachment(type="plan_mode", content=content)


def build_plan_content_attachment(plan_file_path: str) -> Attachment | None:
    """Read the plan file from disk and wrap as an attachment (for post-compact recovery)."""
    if not os.path.exists(plan_file_path):
        return None
    try:
        with open(plan_file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    if not content.strip():
        return None
    return Attachment(
        type="system_reminder",
        content=(
            f"<system-reminder>\n"
            f"Current plan file content ({plan_file_path}):\n\n"
            f"{content}\n"
            f"</system-reminder>"
        ),
    )


def build_plan_exit_attachment(plan_file_path: str) -> Attachment:
    """One-time attachment injected the turn after exiting plan mode."""
    plan_exists = os.path.exists(plan_file_path)
    plan_ref = (
        f" The plan file is located at {plan_file_path} if you need to reference it."
        if plan_exists
        else ""
    )
    content = (
        "<system-reminder>\n"
        "## Exited Plan Mode\n\n"
        "You have exited plan mode. You can now make edits, run tools, "
        f"and take actions.{plan_ref}\n"
        "</system-reminder>"
    )
    return Attachment(type="system_reminder", content=content)


def make_plan_mode_overrides(plan_file_path: str, registry) -> dict[str, ToolDef]:
    """Create tool_overrides that restrict write_file/edit_file to the plan file only.

    NOT currently wired into the main agent loop. Plan mode allows free file
    writes because enforcing plan-file-only restriction would prevent the LLM
    from doing exploratory reads/writes during planning. Kept as opt-in utility
    for stricter plan mode configurations in the future.
    """
    abs_plan = os.path.normpath(os.path.abspath(plan_file_path))
    overrides: dict[str, ToolDef] = {}

    for tool_name in ("write_file", "edit_file"):
        original = registry.get(tool_name)
        if original is None:
            continue
        overrides[tool_name] = _wrap_with_path_guard(original, abs_plan, plan_file_path)

    return overrides


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _wrap_with_path_guard(original: ToolDef, abs_plan: str, display_path: str) -> ToolDef:
    """Wrap a file-writing tool to only allow writes to the plan file."""

    async def _guarded_executor(inputs: dict, ctx) -> ToolResult:
        target = os.path.normpath(os.path.abspath(inputs.get("file_path", "")))
        if target != abs_plan:
            return ToolResult(data={
                "type": "error",
                "content": (
                    f"Plan mode: writes are restricted to the plan file "
                    f"({display_path}). Cannot write to {inputs.get('file_path')}."
                ),
            })
        return await original.executor(inputs, ctx)

    return ToolDef(
        schema=original.schema,
        executor=_guarded_executor,
        map_result=original.map_result,
        display_result=original.display_result,
        is_read_only=original.is_read_only,
    )
