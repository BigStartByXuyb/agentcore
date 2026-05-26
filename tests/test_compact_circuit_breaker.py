"""Tests for auto-compact circuit breaker, blocking limit, and PTL retry."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.compact.auto_compact import (
    auto_compact,
    is_at_blocking_limit,
    should_auto_compact,
    MAX_CONSECUTIVE_COMPACT_FAILURES,
    BLOCKING_LIMIT_BUFFER_TOKENS,
)
from src.errors import is_prompt_too_long
from src.types import MessageHistory, Message, TextContent


# ---------------------------------------------------------------------------
# is_at_blocking_limit
# ---------------------------------------------------------------------------

class TestBlockingLimit:
    def test_below_limit(self):
        assert is_at_blocking_limit(100_000) is False

    def test_at_limit(self):
        from src import config
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
# auto_compact with PTL retry
# ---------------------------------------------------------------------------

class TestAutoCompactRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_try(self):
        history = MessageHistory()
        history.add_user("hello")
        history.add_assistant([TextContent(text="world")])

        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "Summary: conversation about greetings"
        mock_response.content = [mock_block]

        with patch("src.compact.auto_compact.side_query", new_callable=AsyncMock) as mock_sq:
            mock_sq.return_value = mock_response
            result = await auto_compact(history)

        assert result is True
        assert len(history.messages) == 1  # replaced with summary

    @pytest.mark.asyncio
    async def test_ptl_retry_then_success(self):
        history = MessageHistory()
        history.add_user("msg1")
        history.add_assistant([TextContent(text="resp1")])
        history.add_user("msg2")
        history.add_assistant([TextContent(text="resp2")])
        history.add_user("msg3")
        history.add_assistant([TextContent(text="resp3")])

        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "Summary: three exchanges"
        mock_response.content = [mock_block]

        call_count = 0

        async def _side_query(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("prompt is too long: 200000 > 128000")
            return mock_response

        with patch("src.compact.auto_compact.side_query", side_effect=_side_query):
            result = await auto_compact(history)

        assert result is True
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_ptl_all_retries_exhausted(self):
        history = MessageHistory()
        # Need enough messages to form multiple groups
        for i in range(10):
            history.add_user(f"msg{i}")
            history.add_assistant([TextContent(text=f"resp{i}")])

        async def _always_ptl(**kwargs):
            raise Exception("prompt is too long")

        with patch("src.compact.auto_compact.side_query", side_effect=_always_ptl):
            result = await auto_compact(history)

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
            raise Exception("connection refused")

        with patch("src.compact.auto_compact.side_query", side_effect=_fail):
            result = await auto_compact(history)

        assert result is False
        # 2 calls: primary (with thinking) + fallback (without thinking)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_empty_history(self):
        history = MessageHistory()
        result = await auto_compact(history)
        assert result is False


# ---------------------------------------------------------------------------
# MAX_CONSECUTIVE_COMPACT_FAILURES constant
# ---------------------------------------------------------------------------

class TestConstants:
    def test_max_failures_is_3(self):
        assert MAX_CONSECUTIVE_COMPACT_FAILURES == 3
