"""In-memory task store — LLM self-managed todo list.

V1: single agent, memory-only. Tasks are lost on process exit.
Corresponds to Claude Code's TaskStore (simplified).
"""

from __future__ import annotations

from src.types import TaskItem, TaskStatus


class TaskStore:
    """CRUD store for TaskItem — one per agent session."""

    def __init__(self) -> None:
        self._tasks: dict[int, TaskItem] = {}
        self._next_id: int = 1

    def create(self, content: str, active_form: str = "") -> TaskItem:
        task = TaskItem(id=self._next_id, content=content, active_form=active_form)
        self._tasks[self._next_id] = task
        self._next_id += 1
        return task

    def update(
        self,
        task_id: int,
        *,
        status: TaskStatus | None = None,
        content: str | None = None,
        active_form: str | None = None,
    ) -> TaskItem | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if status is not None:
            task.status = status
        if content is not None:
            task.content = content
        if active_form is not None:
            task.active_form = active_form
        return task

    def delete(self, task_id: int) -> bool:
        return self._tasks.pop(task_id, None) is not None

    def get(self, task_id: int) -> TaskItem | None:
        return self._tasks.get(task_id)

    def list_all(self) -> list[TaskItem]:
        return list(self._tasks.values())

    def has_active(self) -> bool:
        return any(t.status != "completed" for t in self._tasks.values())

    def clear(self) -> None:
        self._tasks.clear()
        self._next_id = 1
