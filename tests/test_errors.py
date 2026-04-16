"""Tests for error handling (errors.py, tool_runner.py, messages.py, agent_loop.py).

Covers:
  1. errors.py — classify_api_error + create_assistant_error_message
  2. messages.py — build_tool_result_content with is_error
  3. tool_runner.py — try/catch on executor / map_result, 3-tuple return
  4. agent_loop.py — API error recovery + orphan tool_use recovery
"""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from anthropic import APIError, APIConnectionError

from src.errors import AgentErrorCode, classify_api_error, create_assistant_error_message
from src.messages import build_tool_result_content
from src.types import ToolResult, ToolUseContext


def _drain(gen_or_value):
    """Drain a generator and return its final value, or return the value directly."""
    if hasattr(gen_or_value, '__next__'):
        try:
            while True:
                next(gen_or_value)
        except StopIteration as e:
            return e.value
    return gen_or_value


# =========================================================================
# 1. errors.py — classify_api_error
# =========================================================================

def _make_api_error(status_code: int, message: str = "test") -> APIError:
    """Create a mock APIError with a given status code."""
    err = MagicMock(spec=APIError)
    err.status_code = status_code
    err.status = status_code
    err.__class__ = APIError
    # Make isinstance() work
    return err


class TestClassifyApiError:
    def test_connection_error(self):
        err = APIConnectionError(request=MagicMock())
        assert classify_api_error(err) == AgentErrorCode.API_CONNECTION_ERROR

    def test_connection_timeout(self):
        err = APIConnectionError(request=MagicMock())
        err.__str__ = lambda self: "Connection timed out"
        # Use a real object with timeout in str
        class TimeoutConnError(APIConnectionError):
            def __str__(self):
                return "Connection timed out"
        terr = TimeoutConnError(request=MagicMock())
        assert classify_api_error(terr) == AgentErrorCode.API_TIMEOUT

    def test_rate_limit_429(self):
        err = MagicMock(spec=APIError)
        err.status_code = 429
        # Patch isinstance
        with patch("src.errors.isinstance", side_effect=lambda obj, cls: cls == APIError or cls in (APIError,)):
            # Use direct approach
            pass
        # Simpler: just test classify logic directly
        result = classify_api_error(err)
        assert result == AgentErrorCode.API_RATE_LIMIT

    def test_overloaded_529(self):
        err = MagicMock(spec=APIError)
        err.status_code = 529
        assert classify_api_error(err) == AgentErrorCode.API_OVERLOADED

    def test_auth_401(self):
        err = MagicMock(spec=APIError)
        err.status_code = 401
        assert classify_api_error(err) == AgentErrorCode.API_AUTH_ERROR

    def test_auth_403(self):
        err = MagicMock(spec=APIError)
        err.status_code = 403
        assert classify_api_error(err) == AgentErrorCode.API_AUTH_ERROR

    def test_bad_request_400(self):
        err = MagicMock(spec=APIError)
        err.status_code = 400
        assert classify_api_error(err) == AgentErrorCode.API_BAD_REQUEST

    def test_server_error_500(self):
        err = MagicMock(spec=APIError)
        err.status_code = 500
        assert classify_api_error(err) == AgentErrorCode.API_SERVER_ERROR

    def test_server_error_502(self):
        err = MagicMock(spec=APIError)
        err.status_code = 502
        assert classify_api_error(err) == AgentErrorCode.API_SERVER_ERROR

    def test_unknown_api_error(self):
        err = MagicMock(spec=APIError)
        err.status_code = 418  # I'm a teapot
        assert classify_api_error(err) == AgentErrorCode.API_UNKNOWN

    def test_generic_timeout_exception(self):
        err = Exception("request timed out after 30s")
        assert classify_api_error(err) == AgentErrorCode.API_TIMEOUT

    def test_generic_unknown_exception(self):
        err = Exception("something went wrong")
        assert classify_api_error(err) == AgentErrorCode.API_UNKNOWN


# =========================================================================
# 2. errors.py — create_assistant_error_message
# =========================================================================

class TestCreateAssistantErrorMessage:
    def test_message_structure(self):
        err = Exception("test error")
        msg = create_assistant_error_message(err)
        assert msg["role"] == "assistant"
        assert isinstance(msg["content"], list)
        assert len(msg["content"]) == 1
        assert msg["content"][0]["type"] == "text"

    def test_includes_error_details(self):
        err = Exception("connection refused")
        msg = create_assistant_error_message(err)
        text = msg["content"][0]["text"]
        assert "connection refused" in text
        assert "Exception" in text

    def test_auth_error_message(self):
        err = MagicMock(spec=APIError)
        err.status_code = 401
        err.__class__ = APIError
        msg = create_assistant_error_message(err)
        text = msg["content"][0]["text"]
        assert "ANTHROPIC_AUTH_TOKEN" in text

    def test_rate_limit_message(self):
        err = MagicMock(spec=APIError)
        err.status_code = 429
        err.__class__ = APIError
        msg = create_assistant_error_message(err)
        text = msg["content"][0]["text"]
        assert "429" in text or "Rate limit" in text


# =========================================================================
# 3. messages.py — build_tool_result_content with is_error
# =========================================================================

class TestBuildToolResultContent:
    def test_success_no_is_error(self):
        result = build_tool_result_content("id-1", "output text")
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "id-1"
        assert result["content"] == "output text"
        assert "is_error" not in result

    def test_success_explicit_false(self):
        result = build_tool_result_content("id-1", "output text", is_error=False)
        assert "is_error" not in result

    def test_error_flag(self):
        result = build_tool_result_content("id-1", "something broke", is_error=True)
        assert result["is_error"] is True
        assert "<tool_use_error>" in result["content"]
        assert "</tool_use_error>" in result["content"]
        assert "something broke" in result["content"]


# =========================================================================
# 4. tool_runner.py — run_tool_use 3-tuple return
# =========================================================================

class TestRunToolUse:
    def _make_context(self):
        return ToolUseContext(messages=[], tools=["bash"])

    def test_unknown_tool(self):
        from src.tool_runner import run_tool_use
        result, text, is_error = _drain(run_tool_use("nonexistent", {}, "id-1", self._make_context()))
        assert is_error is True
        assert "No such tool" in text
        assert result.data is None

    def test_executor_exception(self):
        from src.tool_runner import run_tool_use
        from src.tools import ALL_TOOLS
        from src.types import ToolDef

        def bad_executor(inputs, ctx):
            raise ValueError("boom")

        ALL_TOOLS["_test_bad"] = ToolDef(
            schema={"name": "_test_bad"},
            executor=bad_executor,
            map_result=lambda d: str(d),
        )
        try:
            result, text, is_error = _drain(run_tool_use("_test_bad", {}, "id-1", self._make_context()))
            assert is_error is True
            assert "executor failed" in text
            assert "ValueError" in text
            assert "boom" in text
        finally:
            del ALL_TOOLS["_test_bad"]

    def test_map_result_exception(self):
        from src.tool_runner import run_tool_use
        from src.tools import ALL_TOOLS
        from src.types import ToolDef

        def ok_executor(inputs, ctx):
            return ToolResult(data={"ok": True})

        def bad_map(data):
            raise TypeError("can't format")

        ALL_TOOLS["_test_bad_map"] = ToolDef(
            schema={"name": "_test_bad_map"},
            executor=ok_executor,
            map_result=bad_map,
        )
        try:
            result, text, is_error = _drain(run_tool_use("_test_bad_map", {}, "id-1", self._make_context()))
            assert is_error is True
            assert "map_result failed" in text
            assert "TypeError" in text
        finally:
            del ALL_TOOLS["_test_bad_map"]

    def test_success_returns_false(self):
        from src.tool_runner import run_tool_use
        from src.tools import ALL_TOOLS
        from src.types import ToolDef

        ALL_TOOLS["_test_ok"] = ToolDef(
            schema={"name": "_test_ok"},
            executor=lambda i, c: ToolResult(data="hello"),
            map_result=lambda d: d,
        )
        try:
            result, text, is_error = _drain(run_tool_use("_test_ok", {}, "id-1", self._make_context()))
            assert is_error is False
            assert text == "hello"
        finally:
            del ALL_TOOLS["_test_ok"]


# =========================================================================
# 5. agent_loop.py — _recover_orphan_tool_results
# =========================================================================

class TestRecoverOrphanToolResults:
    def test_no_orphans(self):
        from src.agent_loop import _recover_orphan_tool_results
        assistant_content = [
            {"type": "tool_use", "id": "t1", "name": "bash", "input": {}},
        ]
        tool_results = [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        ]
        _recover_orphan_tool_results(assistant_content, tool_results)
        assert len(tool_results) == 1

    def test_one_orphan(self):
        from src.agent_loop import _recover_orphan_tool_results
        assistant_content = [
            {"type": "tool_use", "id": "t1", "name": "bash", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "read_file", "input": {}},
        ]
        tool_results = [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        ]
        _recover_orphan_tool_results(assistant_content, tool_results)
        assert len(tool_results) == 2
        orphan = [r for r in tool_results if r["tool_use_id"] == "t2"][0]
        assert orphan["is_error"] is True
        assert "<tool_use_error>" in orphan["content"]

    def test_no_tool_use_blocks(self):
        from src.agent_loop import _recover_orphan_tool_results
        assistant_content = [
            {"type": "text", "text": "hello"},
        ]
        tool_results = []
        _recover_orphan_tool_results(assistant_content, tool_results)
        assert len(tool_results) == 0


# =========================================================================
# 6. agent_loop.py — API error recovery (integration-style mock test)
# =========================================================================

class TestAgentLoopApiErrorRecovery:
    @patch("src.agent_loop.query_model")
    def test_api_error_injects_synthetic_message(self, mock_query):
        """When query_model raises, agent_loop should return error text
        and keep history alternation correct."""
        from src.agent_loop import run_agent_loop
        from src.types import ToolUseContext, AgentState
        from src.display import consume_events

        mock_query.side_effect = APIConnectionError(request=MagicMock())

        messages = [{"role": "user", "content": "hello"}]
        result = consume_events(run_agent_loop(
            messages=messages,
            system_prompt="test",
            tools=[],
            tool_use_context=ToolUseContext(messages=messages, tools=[]),
            max_turns=3,
            state=AgentState(agent_id="test"),
            label="test",
        ))

        # Should return error text, not raise
        assert "API" in result or "connect" in result.lower()

        # History should end with assistant message (synthetic)
        assert len(messages) >= 2
        assert messages[-1]["role"] == "assistant"

    @patch("src.agent_loop.query_model")
    def test_api_error_does_not_loop(self, mock_query):
        """API error should immediately return, not retry inside the loop."""
        from src.agent_loop import run_agent_loop
        from src.types import ToolUseContext, AgentState
        from src.display import consume_events

        mock_query.side_effect = Exception("unexpected boom")

        messages = [{"role": "user", "content": "test"}]
        result = consume_events(run_agent_loop(
            messages=messages,
            system_prompt="test",
            tools=[],
            tool_use_context=ToolUseContext(messages=messages, tools=[]),
            max_turns=10,
            state=AgentState(agent_id="test"),
            label="test",
        ))

        # query_model should only be called once (no retry inside agent loop)
        assert mock_query.call_count == 1
        assert "unexpected boom" in result
