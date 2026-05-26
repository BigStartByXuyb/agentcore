"""Tests for ExitPlanMode tool."""

import os
import tempfile
from unittest import mock

import pytest

from src.tools.exit_plan_mode import tool, SCHEMA, check_permissions
from src.core.types import AgentState, PlanPhase


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_name(self):
        assert SCHEMA["name"] == "ExitPlanMode"

    def test_no_required_params(self):
        assert SCHEMA["input_schema"]["required"] == []


# ---------------------------------------------------------------------------
# check_permissions
# ---------------------------------------------------------------------------

class TestCheckPermissions:
    def test_returns_ask_when_active(self):
        ctx = mock.MagicMock()
        ctx.agent_state = AgentState(plan_phase=PlanPhase.ACTIVE, plan_file_path="/tmp/p.md")
        result = check_permissions({}, ctx)
        assert result is not None
        assert result.behavior == "ask"
        assert result.custom_prompt is not None

    def test_returns_deny_when_inactive(self):
        ctx = mock.MagicMock()
        ctx.agent_state = AgentState()
        result = check_permissions({}, ctx)
        assert result.behavior == "deny"

    def test_returns_deny_without_state(self):
        ctx = mock.MagicMock()
        ctx.agent_state = None
        result = check_permissions({}, ctx)
        assert result.behavior == "deny"


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class TestExecutor:
    @pytest.mark.asyncio
    async def test_returns_error_without_agent_state(self):
        ctx = mock.MagicMock()
        ctx.agent_state = None
        result = await tool.executor({}, ctx)
        assert result.data["type"] == "error"

    @pytest.mark.asyncio
    async def test_returns_error_when_inactive(self):
        ctx = mock.MagicMock()
        ctx.agent_state = AgentState()
        result = await tool.executor({}, ctx)
        assert result.data["type"] == "error"

    @pytest.mark.asyncio
    async def test_returns_error_for_missing_file(self):
        ctx = mock.MagicMock()
        ctx.agent_state = AgentState(
            plan_phase=PlanPhase.ACTIVE,
            plan_file_path="/nonexistent/plan.md",
        )
        result = await tool.executor({}, ctx)
        assert result.data["type"] == "error"

    @pytest.mark.asyncio
    async def test_reads_plan_content(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Plan\n\n1. Step one\n")
            path = f.name
        try:
            ctx = mock.MagicMock()
            ctx.agent_state = AgentState(
                plan_phase=PlanPhase.ACTIVE,
                plan_file_path=path,
            )
            result = await tool.executor({}, ctx)
            assert result.data["type"] == "plan_complete"
            assert "Step one" in result.data["plan_content"]
            assert result.data["plan_file_path"] == path
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_sets_exiting_on_approval(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Plan\n")
            path = f.name
        try:
            state = AgentState(
                plan_phase=PlanPhase.ACTIVE,
                plan_file_path=path,
            )
            ctx = mock.MagicMock()
            ctx.agent_state = state

            await tool.executor({}, ctx)
            assert state.plan_phase == PlanPhase.EXITING
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# map_result
# ---------------------------------------------------------------------------

class TestMapResult:
    def test_error_result(self):
        text = tool.map_result({"type": "error", "content": "fail"})
        assert "fail" in text

    def test_plan_complete_result(self):
        text = tool.map_result({
            "type": "plan_complete",
            "plan_file_path": "/tmp/plan.md",
            "plan_content": "my plan",
        })
        assert "plan.md" in text
        assert "my plan" in text


# ---------------------------------------------------------------------------
# is_read_only
# ---------------------------------------------------------------------------

class TestIsReadOnly:
    def test_is_read_only(self):
        assert tool.is_read_only({}) is True
