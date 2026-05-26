"""Tests for Plan Mode — state management and attachments."""

import os
import tempfile
from unittest import mock

import pytest

from src.plan_mode import (
    enter_plan_mode,
    build_plan_mode_attachment,
    build_plan_content_attachment,
    build_plan_exit_attachment,
    make_plan_mode_overrides,
    clear_slug_cache,
)
from src.core.types import AgentState, PlanPhase, ToolResult


# ---------------------------------------------------------------------------
# PlanPhase on AgentState
# ---------------------------------------------------------------------------

class TestPlanPhase:
    def test_default_is_inactive(self):
        s = AgentState()
        assert s.plan_phase == PlanPhase.INACTIVE
        assert s.plan_file_path is None

    def test_transition_active_to_exiting(self):
        s = AgentState(plan_phase=PlanPhase.ACTIVE, plan_file_path="/tmp/plan.md")
        s.plan_phase = PlanPhase.EXITING
        assert s.plan_phase == PlanPhase.EXITING
        assert s.plan_file_path == "/tmp/plan.md"

    def test_transition_exiting_to_inactive(self):
        s = AgentState(plan_phase=PlanPhase.EXITING, plan_file_path="/tmp/plan.md")
        s.plan_phase = PlanPhase.INACTIVE
        assert s.plan_phase == PlanPhase.INACTIVE


# ---------------------------------------------------------------------------
# enter_plan_mode
# ---------------------------------------------------------------------------

class TestEnterPlanMode:
    def test_returns_str(self):
        path = enter_plan_mode()
        assert isinstance(path, str)

    def test_path_is_absolute(self):
        path = enter_plan_mode()
        assert os.path.isabs(path)

    def test_path_ends_with_md(self):
        path = enter_plan_mode()
        assert path.endswith(".md")

    def test_plan_dir_created(self):
        path = enter_plan_mode()
        plan_dir = os.path.dirname(path)
        assert os.path.isdir(plan_dir)

    def test_same_session_reuses_path(self):
        """Same session_id reuses the same plan file path."""
        sid = "test-reuse-session"
        s1 = enter_plan_mode(session_id=sid)
        s2 = enter_plan_mode(session_id=sid)
        assert s1 == s2
        clear_slug_cache(sid)

    def test_different_sessions_get_different_paths(self):
        """Different session_ids get different plan file paths."""
        s1 = enter_plan_mode(session_id="session-a")
        s2 = enter_plan_mode(session_id="session-b")
        assert s1 != s2
        clear_slug_cache("session-a")
        clear_slug_cache("session-b")

    def test_clear_slug_cache_resets(self):
        """After clearing cache, a new slug is generated."""
        sid = "test-clear-session"
        s1 = enter_plan_mode(session_id=sid)
        clear_slug_cache(sid)
        s2 = enter_plan_mode(session_id=sid)
        assert s1 != s2
        clear_slug_cache(sid)


# ---------------------------------------------------------------------------
# build_plan_mode_attachment
# ---------------------------------------------------------------------------

class TestBuildPlanModeAttachment:
    def test_returns_attachment(self):
        att = build_plan_mode_attachment("/tmp/test-plan.md")
        assert att.type == "plan_mode"
        assert "/tmp/test-plan.md" in att.content

    def test_contains_constraints(self):
        att = build_plan_mode_attachment("/tmp/test-plan.md")
        assert "ExitPlanMode" in att.content
        assert "plan" in att.content.lower()


# ---------------------------------------------------------------------------
# build_plan_content_attachment
# ---------------------------------------------------------------------------

class TestBuildPlanContentAttachment:
    def test_returns_none_for_missing_file(self):
        att = build_plan_content_attachment("/nonexistent/path.md")
        assert att is None

    def test_returns_none_for_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("")
            path = f.name
        try:
            att = build_plan_content_attachment(path)
            assert att is None
        finally:
            os.unlink(path)

    def test_returns_attachment_with_content(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# My Plan\n\n1. Step one\n2. Step two\n")
            path = f.name
        try:
            att = build_plan_content_attachment(path)
            assert att is not None
            assert att.type == "system_reminder"
            assert "My Plan" in att.content
            assert "Step one" in att.content
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# build_plan_exit_attachment
# ---------------------------------------------------------------------------

class TestBuildPlanExitAttachment:
    def test_returns_system_reminder(self):
        att = build_plan_exit_attachment("/tmp/plan.md")
        assert att.type == "system_reminder"

    def test_contains_exit_message(self):
        att = build_plan_exit_attachment("/tmp/plan.md")
        assert "exited plan mode" in att.content.lower()

    def test_includes_path_when_file_exists(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Plan")
            path = f.name
        try:
            att = build_plan_exit_attachment(path)
            assert path in att.content
        finally:
            os.unlink(path)

    def test_no_path_when_file_missing(self):
        att = build_plan_exit_attachment("/nonexistent/plan-abc.md")
        assert "/nonexistent/plan-abc.md" not in att.content


# ---------------------------------------------------------------------------
# make_plan_mode_overrides
# ---------------------------------------------------------------------------

class TestMakePlanModeOverrides:
    @pytest.fixture
    def plan_file(self, tmp_path):
        return str(tmp_path / "test-plan.md")

    @pytest.fixture
    def mock_registry(self):
        """Minimal mock registry with write_file and edit_file."""
        from src.core.types import ToolDef

        async def _write_exec(inputs, ctx):
            return ToolResult(data={"type": "text", "content": "written"})

        async def _edit_exec(inputs, ctx):
            return ToolResult(data={"type": "text", "content": "edited"})

        write_tool = ToolDef(
            schema={"name": "write_file"},
            executor=_write_exec,
            map_result=lambda d: str(d),
        )
        edit_tool = ToolDef(
            schema={"name": "edit_file"},
            executor=_edit_exec,
            map_result=lambda d: str(d),
        )

        class FakeRegistry:
            def get(self, name):
                if name == "write_file":
                    return write_tool
                if name == "edit_file":
                    return edit_tool
                return None

        return FakeRegistry()

    def test_returns_overrides_for_write_and_edit(self, plan_file, mock_registry):
        overrides = make_plan_mode_overrides(plan_file, mock_registry)
        assert "write_file" in overrides
        assert "edit_file" in overrides

    @pytest.mark.asyncio
    async def test_allows_write_to_plan_file(self, plan_file, mock_registry):
        overrides = make_plan_mode_overrides(plan_file, mock_registry)
        write_override = overrides["write_file"]
        result = await write_override.executor(
            {"file_path": plan_file, "content": "test"},
            mock.MagicMock(),
        )
        assert result.data["type"] == "text"

    @pytest.mark.asyncio
    async def test_blocks_write_to_other_file(self, plan_file, mock_registry):
        overrides = make_plan_mode_overrides(plan_file, mock_registry)
        write_override = overrides["write_file"]
        result = await write_override.executor(
            {"file_path": "/some/other/file.py", "content": "bad"},
            mock.MagicMock(),
        )
        assert result.data["type"] == "error"
        assert "plan" in result.data["content"].lower()

    @pytest.mark.asyncio
    async def test_blocks_edit_to_other_file(self, plan_file, mock_registry):
        overrides = make_plan_mode_overrides(plan_file, mock_registry)
        edit_override = overrides["edit_file"]
        result = await edit_override.executor(
            {"file_path": "/some/other/file.py", "old_string": "a", "new_string": "b"},
            mock.MagicMock(),
        )
        assert result.data["type"] == "error"
