"""TaskCreate tool — create a new task in the LLM's self-managed todo list."""

from __future__ import annotations

from src.core.types import ToolDef, ToolResult, ToolPermissionResult, ToolUseContext

SCHEMA: dict = {
    "name": "TaskCreate",
    "description": (
        "Create a new task to track progress on complex work. "
        "Use this to break down multi-step tasks into trackable items. "
        "Each task starts with status 'pending'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Description of the task to create.",
            },
            "active_form": {
                "type": "string",
                "description": (
                    "Present-tense form shown while the task is in progress "
                    "(e.g. 'Writing unit tests')."
                ),
            },
        },
        "required": ["content"],
    },
}


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    content = inputs.get("content", "").strip()
    if not content:
        return ToolResult(data={"error": "Task content is required."})

    store = getattr(context, "task_store", None)
    if store is None:
        return ToolResult(data={"error": "TaskStore not available."})

    active_form = inputs.get("active_form", "")
    task = store.create(content, active_form=active_form)
    return ToolResult(data={
        "id": task.id,
        "content": task.content,
        "status": task.status,
        "active_form": task.active_form,
    })


def map_result(data: dict) -> str:
    if "error" in data:
        return data["error"]
    return f"Created task #{data['id']}: {data['content']}"


def check_permissions(_inputs: dict, _ctx: ToolUseContext) -> ToolPermissionResult:
    return ToolPermissionResult(behavior="allow")


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    check_permissions=check_permissions,
)
