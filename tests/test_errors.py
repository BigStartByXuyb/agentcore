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

from src.core.errors import (
    AgentErrorCode,
    classify_api_error,
    create_assistant_error_message,
    parse_ptl_token_counts,
    get_ptl_token_gap,
)
from src.messages import build_tool_result_content
from src.core.types import ToolResult, ToolUseContext


import asyncio as _asyncio

def _run_tool(label, tool_name, tool_input, tool_id, context):
    """Run run_tool_use synchronously for tests, return (ToolResult, llm_text, is_error)."""
    from src.tool_runner import run_tool_use
    result, _id, text, is_error = _asyncio.run(
        run_tool_use(label, tool_id, tool_name, tool_input, context, lambda _: None)
    )
    return result, text, is_error


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
        with patch("src.core.errors.isinstance", side_effect=lambda obj, cls: cls == APIError or cls in (APIError,)):
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
# 1b. errors.py — parse_ptl_token_counts / get_ptl_token_gap
# =========================================================================

class TestParsePtlTokenCounts:
    def test_standard_format(self):
        actual, limit = parse_ptl_token_counts(
            "prompt is too long: 137500 tokens > 135000 maximum"
        )
        assert actual == 137500
        assert limit == 135000

    def test_no_units_suffix(self):
        actual, limit = parse_ptl_token_counts(
            "prompt is too long: 200000 token > 128000"
        )
        assert actual == 200000
        assert limit == 128000

    def test_case_insensitive(self):
        actual, limit = parse_ptl_token_counts(
            "Prompt Is Too Long: 150000 Tokens > 100000"
        )
        assert actual == 150000
        assert limit == 100000

    def test_unparseable_no_numbers(self):
        actual, limit = parse_ptl_token_counts("prompt is too long")
        assert actual is None
        assert limit is None

    def test_unrelated_message(self):
        actual, limit = parse_ptl_token_counts("connection refused")
        assert actual is None
        assert limit is None

    def test_context_length_exceeded_no_match(self):
        actual, limit = parse_ptl_token_counts("context_length_exceeded")
        assert actual is None
        assert limit is None

    def test_openai_format(self):
        actual, limit = parse_ptl_token_counts(
            "This model's maximum context length is 65536 tokens. "
            "However, your messages resulted in 70000 tokens."
        )
        assert actual == 70000
        assert limit == 65536

    def test_openai_format_case_insensitive(self):
        actual, limit = parse_ptl_token_counts(
            "Maximum Context Length Is 32000 Tokens. "
            "However, Your Messages Resulted In 40000 Tokens."
        )
        assert actual == 40000
        assert limit == 32000


class TestGetPtlTokenGap:
    def test_gap_calculation(self):
        err = Exception("prompt is too long: 150000 tokens > 128000 maximum")
        gap = get_ptl_token_gap(err)
        assert gap == 22000

    def test_unparseable_returns_none(self):
        err = Exception("prompt is too long")
        gap = get_ptl_token_gap(err)
        assert gap is None

    def test_zero_gap_returns_none(self):
        err = Exception("prompt is too long: 128000 tokens > 128000")
        gap = get_ptl_token_gap(err)
        assert gap is None

    def test_swapped_numbers_still_yields_gap(self):
        # Normalization always puts bigger number as actual
        err = Exception("prompt is too long: 100000 tokens > 128000")
        gap = get_ptl_token_gap(err)
        assert gap == 28000


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
        assert result.type == "tool_result"
        assert result.tool_use_id == "id-1"
        assert result.content == "output text"
        assert result.is_error is False

    def test_success_explicit_false(self):
        result = build_tool_result_content("id-1", "output text", is_error=False)
        assert result.is_error is False

    def test_error_flag(self):
        result = build_tool_result_content("id-1", "something broke", is_error=True)
        assert result.is_error is True
        assert "<tool_use_error>" in result.content
        assert "</tool_use_error>" in result.content
        assert "something broke" in result.content


# =========================================================================
# 4. tool_runner.py — run_tool_use 3-tuple return
# =========================================================================

class TestRunToolUse:
    def _make_context(self, extra_tools=None):
        from src.tools import registry
        from src.permissions import PermissionEngine, PermissionRule
        tools = list(registry.list_names())
        if extra_tools:
            tools.extend(extra_tools)
        perm = PermissionEngine()
        perm.add_session_rule(PermissionRule(tool_name="*", content_pattern=None, behavior="allow", source="session"))
        return ToolUseContext(messages=[], tools=tools, permissions=perm)

    def test_unknown_tool(self):
        result, text, is_error = _run_tool("test", "nonexistent", {}, "id-1", self._make_context())
        assert is_error is True
        assert "No such tool" in text
        assert result.data is None

    def test_executor_exception(self):
        from src.tools import registry
        from src.core.types import ToolDef

        async def bad_executor(inputs, ctx):
            raise ValueError("boom")

        registry.register("_test_bad", ToolDef(
            schema={"name": "_test_bad"},
            executor=bad_executor,
            map_result=lambda d: str(d),
        ))
        try:
            result, text, is_error = _run_tool("test", "_test_bad", {}, "id-1", self._make_context())
            assert is_error is True
            assert "executor failed" in text
            assert "ValueError" in text
            assert "boom" in text
        finally:
            registry.unregister("_test_bad")

    def test_map_result_exception(self):
        from src.tools import registry
        from src.core.types import ToolDef

        async def ok_executor(inputs, ctx):
            return ToolResult(data={"ok": True})

        def bad_map(data):
            raise TypeError("can't format")

        registry.register("_test_bad_map", ToolDef(
            schema={"name": "_test_bad_map"},
            executor=ok_executor,
            map_result=bad_map,
        ))
        try:
            result, text, is_error = _run_tool("test", "_test_bad_map", {}, "id-1", self._make_context())
            assert is_error is True
            assert "map_result failed" in text
            assert "TypeError" in text
        finally:
            registry.unregister("_test_bad_map")

    def test_success_returns_false(self):
        from src.tools import registry
        from src.core.types import ToolDef

        registry.register("_test_ok", ToolDef(
            schema={"name": "_test_ok"},
            executor=lambda i, c: ToolResult(data="hello"),
            map_result=lambda d: d,
        ))
        try:
            result, text, is_error = _run_tool("test", "_test_ok", {}, "id-1", self._make_context())
            assert is_error is False
            assert text == "hello"
        finally:
            registry.unregister("_test_ok")


# =========================================================================
# 5. agent_loop.py — _recover_orphan_tool_results
# =========================================================================

class TestRecoverOrphanToolResults:
    def test_no_orphans(self):
        from src.agent_loop import _recover_orphan_tool_results
        from src.core.types import ToolUseContent, ToolResultContent
        assistant_content = [
            ToolUseContent(id="t1", name="bash", input={}),
        ]
        tool_results = [
            ToolResultContent(tool_use_id="t1", content="ok"),
        ]
        _recover_orphan_tool_results(assistant_content, tool_results)
        assert len(tool_results) == 1

    def test_one_orphan(self):
        from src.agent_loop import _recover_orphan_tool_results
        from src.core.types import ToolUseContent, ToolResultContent
        assistant_content = [
            ToolUseContent(id="t1", name="bash", input={}),
            ToolUseContent(id="t2", name="read_file", input={}),
        ]
        tool_results = [
            ToolResultContent(tool_use_id="t1", content="ok"),
        ]
        _recover_orphan_tool_results(assistant_content, tool_results)
        assert len(tool_results) == 2
        orphan = [r for r in tool_results if r.tool_use_id == "t2"][0]
        assert orphan.is_error is True
        assert "<tool_use_error>" in orphan.content

    def test_no_tool_use_blocks(self):
        from src.agent_loop import _recover_orphan_tool_results
        from src.core.types import TextContent
        assistant_content = [
            TextContent(text="hello"),
        ]
        tool_results = []
        _recover_orphan_tool_results(assistant_content, tool_results)
        assert len(tool_results) == 0


# =========================================================================
# 6. agent_loop.py — API error recovery (integration-style mock test)
# =========================================================================

class TestAgentLoopApiErrorRecovery:
    def test_api_error_injects_synthetic_message(self):
        """When query_model_stream yields an error ProviderMessage, agent_loop
        injects a synthetic error message and continues until max_turns."""
        import asyncio
        from src.agent_loop import run_agent_loop
        from src.core.types import ToolUseContext, AgentState, MessageHistory, Message
        from src.providers.types import ProviderMessage, TextBlock

        call_count = 0

        async def mock_stream(**kwargs):
            nonlocal call_count
            call_count += 1
            yield ProviderMessage(
                content=[TextBlock(text="Connection error.")],
                stop_reason="error",
                is_error=True,
                error_code="api_connection_error",
            )

        history = MessageHistory([Message(role="user", content="hello")])
        with patch("src.agent_loop.query_model_stream", mock_stream):
            result = asyncio.run(run_agent_loop(
                tool_use_context=ToolUseContext(messages=history, tools=[], system_prompt="test", label="test", agent_state=AgentState(agent_id="test")),
                max_turns=3,
                on_event=lambda _: None,
            ))

        assert result.reason == "max_turns"
        assert "max turns" in result.text.lower()
        assert call_count >= 1
        assert len(history.messages) > 1
        error_msgs = [m for m in history.messages if m.role == "assistant"]
        assert len(error_msgs) > 0

    def test_api_error_does_not_crash(self):
        """API errors are handled gracefully — loop reaches max_turns
        without crashing."""
        import asyncio
        from src.agent_loop import run_agent_loop
        from src.core.types import ToolUseContext, AgentState, MessageHistory, Message
        from src.providers.types import ProviderMessage, TextBlock

        call_count = 0

        async def mock_stream(**kwargs):
            nonlocal call_count
            call_count += 1
            yield ProviderMessage(
                content=[TextBlock(text="Unexpected error.")],
                stop_reason="error",
                is_error=True,
                error_code="api_unknown",
            )

        history = MessageHistory([Message(role="user", content="test")])
        with patch("src.agent_loop.query_model_stream", mock_stream):
            result = asyncio.run(run_agent_loop(
                tool_use_context=ToolUseContext(messages=history, tools=[], system_prompt="test", label="test", agent_state=AgentState(agent_id="test")),
                max_turns=2,
                on_event=lambda _: None,
            ))

        assert call_count >= 1
        assert result.reason == "max_turns"
        assert "max turns" in result.text.lower()
