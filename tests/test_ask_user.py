"""Tests for AskUserQuestion tool."""

from unittest import mock

import pytest

from src.tools.ask_user import tool, SCHEMA, check_permissions
from src.core.events import UserQuestionRequest


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_name(self):
        assert SCHEMA["name"] == "AskUserQuestion"

    def test_questions_required(self):
        assert "questions" in SCHEMA["input_schema"]["required"]


# ---------------------------------------------------------------------------
# check_permissions
# ---------------------------------------------------------------------------

class TestCheckPermissions:
    def test_always_allows(self):
        ctx = mock.MagicMock()
        result = check_permissions({}, ctx)
        assert result.behavior == "allow"


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class TestExecutor:
    @pytest.mark.asyncio
    async def test_returns_error_without_questions(self):
        ctx = mock.MagicMock()
        ctx.on_event = mock.MagicMock()
        result = await tool.executor({}, ctx)
        assert result.data["type"] == "error"

    @pytest.mark.asyncio
    async def test_returns_error_for_empty_questions(self):
        ctx = mock.MagicMock()
        ctx.on_event = mock.MagicMock()
        result = await tool.executor({"questions": []}, ctx)
        assert result.data["type"] == "error"

    @pytest.mark.asyncio
    async def test_returns_error_for_invalid_question(self):
        ctx = mock.MagicMock()
        ctx.on_event = mock.MagicMock()
        result = await tool.executor({"questions": [{"header": "Test"}]}, ctx)
        assert result.data["type"] == "error"

    @pytest.mark.asyncio
    async def test_emits_event_and_returns_answers(self):
        captured_events: list = []

        def on_event(ev):
            captured_events.append(ev)
            if isinstance(ev, UserQuestionRequest) and ev.future is not None:
                ev.future.set_result({"Which color?": "Blue"})

        ctx = mock.MagicMock()
        ctx.on_event = on_event

        questions = [{
            "question": "Which color?",
            "header": "Color",
            "options": [
                {"label": "Red", "description": "Warm color"},
                {"label": "Blue", "description": "Cool color"},
            ],
            "multiSelect": False,
        }]

        result = await tool.executor({"questions": questions}, ctx)
        assert result.data["type"] == "answers"
        assert result.data["answers"] == {"Which color?": "Blue"}

        assert len(captured_events) == 1
        assert isinstance(captured_events[0], UserQuestionRequest)
        assert len(captured_events[0].questions) == 1

    @pytest.mark.asyncio
    async def test_multiple_questions(self):
        def on_event(ev):
            if isinstance(ev, UserQuestionRequest) and ev.future is not None:
                ev.future.set_result({
                    "Which color?": "Red",
                    "Which size?": "Large",
                })

        ctx = mock.MagicMock()
        ctx.on_event = on_event

        questions = [
            {
                "question": "Which color?",
                "header": "Color",
                "options": [
                    {"label": "Red", "description": "Warm"},
                    {"label": "Blue", "description": "Cool"},
                ],
                "multiSelect": False,
            },
            {
                "question": "Which size?",
                "header": "Size",
                "options": [
                    {"label": "Small", "description": "Compact"},
                    {"label": "Large", "description": "Full size"},
                ],
                "multiSelect": False,
            },
        ]

        result = await tool.executor({"questions": questions}, ctx)
        assert result.data["type"] == "answers"
        assert result.data["answers"]["Which color?"] == "Red"
        assert result.data["answers"]["Which size?"] == "Large"


# ---------------------------------------------------------------------------
# map_result
# ---------------------------------------------------------------------------

class TestMapResult:
    def test_error_result(self):
        text = tool.map_result({"type": "error", "content": "fail"})
        assert "fail" in text

    def test_answers_result(self):
        text = tool.map_result({
            "type": "answers",
            "answers": {"Which color?": "Blue"},
        })
        assert "Which color?" in text
        assert "Blue" in text

    def test_empty_answers(self):
        text = tool.map_result({"type": "answers", "answers": {}})
        assert "No answers" in text


# ---------------------------------------------------------------------------
# is_read_only
# ---------------------------------------------------------------------------

class TestIsReadOnly:
    def test_is_read_only(self):
        assert tool.is_read_only({}) is True
