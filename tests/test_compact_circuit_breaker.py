"""Tests for auto-compact circuit breaker, blocking limit, and PTL retry."""

import pytest
from unittest.mock import patch

from src.compact.auto_compact import (
    auto_compact,
    is_at_blocking_limit,
    should_auto_compact,
    MAX_CONSECUTIVE_COMPACT_FAILURES,
    BLOCKING_LIMIT_BUFFER_TOKENS,
)
from src.core.errors import is_prompt_too_long
from src.core.types import LoopResult, MessageHistory, Message, TextContent, ToolUseContext


# ---------------------------------------------------------------------------
# is_at_blocking_limit
# ---------------------------------------------------------------------------

class TestBlockingLimit:
    def test_below_limit(self):
        assert is_at_blocking_limit(100_000) is False

    def test_at_limit(self):
        from src.core import config
        threshold = config.MAX_CONTEXT_WINDOW - BLOCKING_LIMIT_BUFFER_TOKENS
        assert is_at_blocking_limit(threshold) is True

    def test_above_limit(self):
        assert is_at_blocking_limit(999_999) is True


# ---------------------------------------------------------------------------
# is_prompt_too_long
# ---------------------------------------------------------------------------

class TestIsPromptTooLong:
    def test_detects_prompt_too_long(self):
        err = Exception("prompt is too long: 150000 > 128000")
        assert is_prompt_too_long(err) is True

    def test_detects_prompt_too_long_variant(self):
        err = Exception("error: prompt_too_long")
        assert is_prompt_too_long(err) is True

    def test_rejects_other_errors(self):
        err = Exception("connection timeout")
        assert is_prompt_too_long(err) is False


# ---------------------------------------------------------------------------
# Helper: mock run_agent_loop response
# ---------------------------------------------------------------------------

def _completed(text: str = "Summary: conversation about greetings") -> LoopResult:
    return LoopResult(reason="completed", text=text)


def _loop_error(text: str = "connection refused") -> LoopResult:
    return LoopResult(reason="error", text=text)


def _ptl(text: str = "prompt is too long") -> LoopResult:
    return LoopResult(reason="prompt_too_long", text=text)


# ---------------------------------------------------------------------------
# auto_compact with PTL retry
# ---------------------------------------------------------------------------

class TestAutoCompactRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_try(self):
        history = MessageHistory()
        history.add_user("hello")
        history.add_assistant([TextContent(text="world")])

        with patch("src.agent_loop.run_agent_loop", return_value=_completed()) as mock_loop:
            result = await auto_compact(ToolUseContext(messages=history, tools=[], system_prompt="test"))

        assert result is True
        assert len(history.messages) == 1  # replaced with summary
        mock_loop.assert_called_once()
        assert mock_loop.call_args.kwargs["query_source"] == "compact"

    @pytest.mark.asyncio
    async def test_ptl_retry_then_success(self):
        history = MessageHistory()
        history.add_user("msg1")
        history.add_assistant([TextContent(text="resp1")])
        history.add_user("msg2")
        history.add_assistant([TextContent(text="resp2")])
        history.add_user("msg3")
        history.add_assistant([TextContent(text="resp3")])

        call_count = 0

        async def _mock_loop(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _ptl("prompt is too long: 200000 > 128000")
            return _completed("Summary: three exchanges")

        with patch("src.agent_loop.run_agent_loop", side_effect=_mock_loop):
            result = await auto_compact(ToolUseContext(messages=history, tools=[], system_prompt="test"))

        assert result is True
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_ptl_all_retries_exhausted(self):
        history = MessageHistory()
        for i in range(10):
            history.add_user(f"msg{i}")
            history.add_assistant([TextContent(text=f"resp{i}")])

        async def _always_ptl(**kwargs):
            return _ptl("prompt is too long")

        with patch("src.agent_loop.run_agent_loop", side_effect=_always_ptl):
            result = await auto_compact(ToolUseContext(messages=history, tools=[], system_prompt="test"))

        assert result is False

    @pytest.mark.asyncio
    async def test_non_ptl_error_no_retry(self):
        history = MessageHistory()
        history.add_user("hello")
        history.add_assistant([TextContent(text="world")])

        call_count = 0

        async def _fail(**kwargs):
            nonlocal call_count
            call_count += 1
            return _loop_error("connection refused")

        with patch("src.agent_loop.run_agent_loop", side_effect=_fail):
            result = await auto_compact(ToolUseContext(messages=history, tools=[], system_prompt="test"))

        assert result is False
        # Only 1 call: non-PTL/non-thinking errors don't trigger thinking fallback
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_empty_history(self):
        history = MessageHistory()
        result = await auto_compact(ToolUseContext(messages=history, tools=[], system_prompt="test"))
        assert result is False


# ---------------------------------------------------------------------------
# Truncation state preservation across thinking fallback
# ---------------------------------------------------------------------------

class TestTruncationStatePreservation:
    @pytest.mark.asyncio
    async def test_ptl_preserves_truncation_across_thinking_fallback(self):
        """When thinking=True gets PTL and truncates, the thinking=False
        fallback should use the SAME truncated messages, not start fresh."""
        history = MessageHistory()
        for i in range(10):
            history.add_user(f"msg{i}")
            history.add_assistant([TextContent(text=f"resp{i}")])

        call_message_counts: list[int] = []
        call_count = 0

        async def _mock_loop(**kwargs):
            nonlocal call_count
            call_count += 1
            msg_count = len(kwargs["tool_use_context"].messages.messages)
            call_message_counts.append(msg_count)
            if call_count == 1:
                return _ptl("prompt is too long: 200000 tokens > 128000")
            # After truncation, succeed
            return _completed("Summary")

        with patch("src.agent_loop.run_agent_loop", side_effect=_mock_loop):
            result = await auto_compact(ToolUseContext(messages=history, tools=[], system_prompt="test"))

        assert result is True
        assert call_count == 2
        # After truncation, message count should be smaller
        assert call_message_counts[1] < call_message_counts[0]

    @pytest.mark.asyncio
    async def test_thinking_ptl_then_no_thinking_success(self):
        """When thinking=True gets PTL but thinking=False succeeds on same messages."""
        history = MessageHistory()
        history.add_user("hello")
        history.add_assistant([TextContent(text="world")])

        call_count = 0

        async def _mock_loop(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _ptl("prompt is too long: 200000 > 128000")
            return _completed("Summary")

        with patch("src.agent_loop.run_agent_loop", side_effect=_mock_loop):
            result = await auto_compact(ToolUseContext(messages=history, tools=[], system_prompt="test"))

        assert result is True
        assert call_count == 2


# ---------------------------------------------------------------------------
# MAX_CONSECUTIVE_COMPACT_FAILURES constant
# ---------------------------------------------------------------------------

class TestConstants:
    def test_max_failures_is_3(self):
        assert MAX_CONSECUTIVE_COMPACT_FAILURES == 3
