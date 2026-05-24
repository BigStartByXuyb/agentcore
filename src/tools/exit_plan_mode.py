"""ExitPlanMode tool — signals plan completion and triggers user approval.

The LLM calls this tool when it has finished writing the plan file.
Approval happens mid-turn via the permission system (check_permissions
returns "ask"), not post-loop. On rejection, the executor never runs
and the LLM receives an error tool_result with user feedback.
"""

from __future__ import annotations

import os

from src.types import ToolDef, ToolPermissionResult, ToolResult, ToolUseContext


SCHEMA: dict = {
    "name": "ExitPlanMode",
    "description": (
        "Submit your completed plan for user review and approval. "
        "ONLY call this after ALL plan mode phases are complete: "
        "(1) you explored the codebase with Explore agents and/or read_file to understand the relevant code, "
        "(2) you asked clarifying questions via AskUserQuestion if anything was ambiguous, "
        "(3) you wrote a full plan to the plan file with all required sections "
        "(Context, files to modify, implementation steps, verification), and "
        "(4) you reviewed the plan against the actual codebase for accuracy. "
        "Do NOT call this prematurely — a shallow plan that just restates the task will be rejected."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def check_permissions(inputs: dict, context: ToolUseContext) -> ToolPermissionResult:
    """Always ask user for plan approval — mirrors Claude Code's ExitPlanMode permission."""
    from src.types import PlanPhase

    state = context.agent_state
    if not state or state.plan_phase != PlanPhase.ACTIVE:
        return ToolPermissionResult(
            behavior="deny",
            deny_message="ExitPlanMode called but no plan mode is active.",
        )
    return ToolPermissionResult(
        behavior="ask",
        custom_prompt="\n  Plan is ready for review. Approve this plan? [y/n]: ",
    )


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    """Read plan content and transition to EXITING.

    Only runs if the permission system approved (user said "yes").
    """
    from src.types import PlanPhase

    state = context.agent_state
    if not state or state.plan_phase != PlanPhase.ACTIVE or not state.plan_file_path:
        return ToolResult(data={
            "type": "error",
            "content": "ExitPlanMode called but no plan mode is active.",
        })

    plan_path = state.plan_file_path

    if not os.path.exists(plan_path):
        return ToolResult(data={
            "type": "error",
            "content": f"Plan file not found: {plan_path}. Write a plan first.",
        })

    try:
        with open(plan_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return ToolResult(data={
            "type": "error",
            "content": f"Failed to read plan file: {e}",
        })

    state.plan_phase = PlanPhase.EXITING

    return ToolResult(data={
        "type": "plan_complete",
        "plan_file_path": plan_path,
        "plan_content": content,
    })


def map_result(data: dict) -> str:
    if data.get("type") == "error":
        return data["content"]
    path = data.get("plan_file_path", "")
    content = data.get("plan_content", "")
    return f"Plan approved. Plan file: {path}\n\n{content}"


def is_read_only(inputs: dict) -> bool:
    return True


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    is_read_only=is_read_only,
    check_permissions=check_permissions,
)
