"""TaskUpdate tool — update or delete an existing task."""

from __future__ import annotations

from src.core.types import ToolDef, ToolResult, ToolPermissionResult, ToolUseContext

SCHEMA: dict = {
    "name": "TaskUpdate",
    "description": (
        "Update an existing task's status, content, or active_form. "
        "Set status to 'deleted' to remove the task entirely."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "integer",
                "description": "The ID of the task to update.",
            },
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "deleted"],
                "description": "New status. Use 'deleted' to remove the task.",
            },
            "content": {
                "type": "string",
                "description": "New task description (optional).",
            },
            "active_form": {
                "type": "string",
                "description": "New present-tense form (optional).",
            },
        },
        "required": ["task_id"],
    },
}


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    task_id = inputs.get("task_id")
    if task_id is None:
        return ToolResult(data={"error": "task_id is required."})

    store = getattr(context, "task_store", None)
    if store is None:
        return ToolResult(data={"error": "TaskStore not available."})

    status = inputs.get("status")
    if status == "deleted":
        ok = store.delete(task_id)
        if not ok:
            return ToolResult(data={"error": f"Task #{task_id} not found."})
        return ToolResult(data={"id": task_id, "deleted": True})

    task = store.update(
        task_id,
        status=status,
        content=inputs.get("content"),
        active_form=inputs.get("active_form"),
    )
    if task is None:
        return ToolResult(data={"error": f"Task #{task_id} not found."})

    return ToolResult(data={
        "id": task.id,
        "content": task.content,
        "status": task.status,
        "active_form": task.active_form,
    })


def map_result(data: dict) -> str:
    if "error" in data:
        return data["error"]
    if data.get("deleted"):
        return f"Deleted task #{data['id']}."
    return f"Updated task #{data['id']}: [{data['status']}] {data['content']}"


def check_permissions(_inputs: dict, _ctx: ToolUseContext) -> ToolPermissionResult:
    return ToolPermissionResult(behavior="allow")


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    check_permissions=check_permissions,
)
