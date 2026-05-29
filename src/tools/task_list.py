"""TaskList tool — list all tasks in the LLM's self-managed todo list."""

from __future__ import annotations

from src.core.types import ToolDef, ToolResult, ToolPermissionResult, ToolUseContext

SCHEMA: dict = {
    "name": "TaskList",
    "description": (
        "List all tasks and their statuses. "
        "Use this to review progress, especially after context compaction."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    store = getattr(context, "task_store", None)
    if store is None:
        from src.task_store import get_task_store
        store = get_task_store()

    tasks = store.list_all()
    return ToolResult(data={
        "tasks": [
            {
                "id": t.id,
                "content": t.content,
                "status": t.status,
                "active_form": t.active_form,
            }
            for t in tasks
        ],
    })


_STATUS_ICONS = {
    "pending": "○",
    "in_progress": "◉",
    "completed": "●",
}


def map_result(data: dict) -> str:
    if "error" in data:
        return data["error"]
    tasks = data.get("tasks", [])
    if not tasks:
        return "No tasks."
    lines: list[str] = []
    for t in tasks:
        icon = _STATUS_ICONS.get(t["status"], "?")
        lines.append(f"{icon} #{t['id']} [{t['status']}] {t['content']}")
    return "\n".join(lines)


def check_permissions(_inputs: dict, _ctx: ToolUseContext) -> ToolPermissionResult:
    return ToolPermissionResult(behavior="allow")


def is_read_only(_inputs: dict) -> bool:
    return True


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    check_permissions=check_permissions,
    is_read_only=is_read_only,
)
