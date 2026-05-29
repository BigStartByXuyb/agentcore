"""Tests for TaskCreate, TaskUpdate, TaskList tools."""

from unittest import mock

import pytest

import src.task_store as task_store_mod
from src.task_store import TaskStore
from src.tools.task_create import tool as create_tool, SCHEMA as CREATE_SCHEMA
from src.tools.task_update import tool as update_tool, SCHEMA as UPDATE_SCHEMA
from src.tools.task_list import tool as list_tool, SCHEMA as LIST_SCHEMA


@pytest.fixture(autouse=True)
def _reset_global_task_store():
    task_store_mod._global_store = None
    yield
    task_store_mod._global_store = None


def _make_context(task_store=None):
    ctx = mock.MagicMock()
    ctx.task_store = task_store
    return ctx


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestSchemas:
    def test_create_name(self):
        assert CREATE_SCHEMA["name"] == "TaskCreate"

    def test_create_requires_content(self):
        assert "content" in CREATE_SCHEMA["input_schema"]["required"]

    def test_update_name(self):
        assert UPDATE_SCHEMA["name"] == "TaskUpdate"

    def test_update_requires_task_id(self):
        assert "task_id" in UPDATE_SCHEMA["input_schema"]["required"]

    def test_list_name(self):
        assert LIST_SCHEMA["name"] == "TaskList"

    def test_list_no_required(self):
        assert "required" not in LIST_SCHEMA["input_schema"]


# ---------------------------------------------------------------------------
# TaskCreate
# ---------------------------------------------------------------------------

class TestTaskCreate:
    @pytest.mark.asyncio
    async def test_creates_task(self):
        store = TaskStore()
        ctx = _make_context(store)
        result = await create_tool.executor({"content": "Build feature X"}, ctx)
        assert result.data["id"] == 1
        assert result.data["content"] == "Build feature X"
        assert result.data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_creates_with_active_form(self):
        store = TaskStore()
        ctx = _make_context(store)
        result = await create_tool.executor(
            {"content": "Write tests", "active_form": "Writing tests"}, ctx,
        )
        assert result.data["active_form"] == "Writing tests"

    @pytest.mark.asyncio
    async def test_empty_content_error(self):
        store = TaskStore()
        ctx = _make_context(store)
        result = await create_tool.executor({"content": "  "}, ctx)
        assert "error" in result.data

    @pytest.mark.asyncio
    async def test_no_store_falls_back_to_global_store(self):
        ctx = _make_context(None)
        result = await create_tool.executor({"content": "Task"}, ctx)
        assert result.data["id"] == 1
        assert result.data["content"] == "Task"

    def test_map_result_success(self):
        text = create_tool.map_result({"id": 1, "content": "Do stuff", "status": "pending"})
        assert "#1" in text
        assert "Do stuff" in text

    def test_map_result_error(self):
        text = create_tool.map_result({"error": "Something wrong"})
        assert "Something wrong" in text

    def test_check_permissions_allow(self):
        result = create_tool.check_permissions({}, mock.MagicMock())
        assert result.behavior == "allow"


# ---------------------------------------------------------------------------
# TaskUpdate
# ---------------------------------------------------------------------------

class TestTaskUpdate:
    @pytest.mark.asyncio
    async def test_update_status(self):
        store = TaskStore()
        store.create("Task A")
        ctx = _make_context(store)
        result = await update_tool.executor({"task_id": 1, "status": "in_progress"}, ctx)
        assert result.data["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_update_content(self):
        store = TaskStore()
        store.create("Old")
        ctx = _make_context(store)
        result = await update_tool.executor({"task_id": 1, "content": "New"}, ctx)
        assert result.data["content"] == "New"

    @pytest.mark.asyncio
    async def test_delete_task(self):
        store = TaskStore()
        store.create("To remove")
        ctx = _make_context(store)
        result = await update_tool.executor({"task_id": 1, "status": "deleted"}, ctx)
        assert result.data["deleted"] is True
        assert store.get(1) is None

    @pytest.mark.asyncio
    async def test_update_nonexistent(self):
        store = TaskStore()
        ctx = _make_context(store)
        result = await update_tool.executor({"task_id": 999, "status": "completed"}, ctx)
        assert "error" in result.data

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        store = TaskStore()
        ctx = _make_context(store)
        result = await update_tool.executor({"task_id": 999, "status": "deleted"}, ctx)
        assert "error" in result.data

    @pytest.mark.asyncio
    async def test_no_task_id(self):
        store = TaskStore()
        ctx = _make_context(store)
        result = await update_tool.executor({}, ctx)
        assert "error" in result.data

    @pytest.mark.asyncio
    async def test_no_store_falls_back_to_global_store(self):
        ctx = _make_context(None)
        result = await update_tool.executor({"task_id": 1}, ctx)
        assert "error" in result.data

    def test_map_result_update(self):
        text = update_tool.map_result({"id": 1, "content": "Task", "status": "completed"})
        assert "#1" in text
        assert "completed" in text

    def test_map_result_delete(self):
        text = update_tool.map_result({"id": 1, "deleted": True})
        assert "Deleted" in text

    def test_map_result_error(self):
        text = update_tool.map_result({"error": "Not found"})
        assert "Not found" in text

    def test_check_permissions_allow(self):
        result = update_tool.check_permissions({}, mock.MagicMock())
        assert result.behavior == "allow"


# ---------------------------------------------------------------------------
# TaskList
# ---------------------------------------------------------------------------

class TestTaskList:
    @pytest.mark.asyncio
    async def test_empty_list(self):
        store = TaskStore()
        ctx = _make_context(store)
        result = await list_tool.executor({}, ctx)
        assert result.data["tasks"] == []

    @pytest.mark.asyncio
    async def test_lists_tasks(self):
        store = TaskStore()
        store.create("A")
        store.create("B")
        ctx = _make_context(store)
        result = await list_tool.executor({}, ctx)
        assert len(result.data["tasks"]) == 2

    @pytest.mark.asyncio
    async def test_no_store_falls_back_to_global_store(self):
        ctx = _make_context(None)
        result = await list_tool.executor({}, ctx)
        assert result.data["tasks"] == []

    def test_map_result_no_tasks(self):
        text = list_tool.map_result({"tasks": []})
        assert text == "No tasks."

    def test_map_result_with_tasks(self):
        text = list_tool.map_result({
            "tasks": [
                {"id": 1, "content": "Task A", "status": "pending"},
                {"id": 2, "content": "Task B", "status": "completed"},
            ],
        })
        assert "#1" in text
        assert "#2" in text
        assert "pending" in text
        assert "completed" in text

    def test_map_result_error(self):
        text = list_tool.map_result({"error": "No store"})
        assert "No store" in text

    def test_is_read_only(self):
        assert list_tool.is_read_only({}) is True

    def test_check_permissions_allow(self):
        result = list_tool.check_permissions({}, mock.MagicMock())
        assert result.behavior == "allow"
