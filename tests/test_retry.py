"""Tests for src.providers.retry — shared retry logic.

Pure Python, no SDK dependencies. Uses custom exceptions to exercise
all retry paths without importing anthropic or openai.
"""

from __future__ import annotations

import asyncio
import pytest

from src.providers.retry import (
    with_retry,
    get_retry_delay,
    RetryEvent,
    DEFAULT_MAX_RETRIES,
    BASE_DELAY_MS,
    MAX_DELAY_MS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class RetryableError(Exception):
    pass

class NonRetryableError(Exception):
    pass

class ConnectionError_(Exception):
    pass


def _is_retryable(e: Exception) -> bool:
    return isinstance(e, RetryableError)


def _extract_retry_after(e: Exception) -> float | None:
    return getattr(e, "retry_after", None)


async def _consume(gen) -> tuple[list[RetryEvent], object]:
    """Consume a with_retry generator, returning (retry_events, result)."""
    events: list[RetryEvent] = []
    result = None
    async for item in gen:
        if isinstance(item, RetryEvent):
            events.append(item)
        else:
            result = item
    return events, result


# ---------------------------------------------------------------------------
# get_retry_delay
# ---------------------------------------------------------------------------

class TestGetRetryDelay:
    def test_retry_after_takes_precedence(self):
        assert get_retry_delay(attempt=1, retry_after=5.0) == 5.0

    def test_exponential_growth(self):
        d1 = get_retry_delay(1)
        d2 = get_retry_delay(2)
        d3 = get_retry_delay(3)
        assert d1 < d2 < d3

    def test_capped_at_max(self):
        delay = get_retry_delay(attempt=100)
        assert delay <= MAX_DELAY_MS / 1000 * 1.25  # max + 25% jitter

    def test_first_attempt_base(self):
        delay = get_retry_delay(attempt=1)
        base_s = BASE_DELAY_MS / 1000
        assert base_s <= delay <= base_s * 1.25


# ---------------------------------------------------------------------------
# with_retry — success on first try
# ---------------------------------------------------------------------------

class TestWithRetrySuccess:
    @pytest.mark.asyncio
    async def test_returns_result_immediately(self):
        async def op():
            return "ok"

        events, result = await _consume(with_retry(
            op,
            is_retryable=_is_retryable,
            extract_retry_after=_extract_retry_after,
            max_retries=3,
            label="test",
        ))
        assert result == "ok"
        assert events == []

    @pytest.mark.asyncio
    async def test_no_retry_events_on_success(self):
        async def op():
            return 42

        events, result = await _consume(with_retry(
            op,
            is_retryable=_is_retryable,
            extract_retry_after=_extract_retry_after,
            label="test",
        ))
        assert result == 42
        assert len(events) == 0


# ---------------------------------------------------------------------------
# with_retry — retryable errors
# ---------------------------------------------------------------------------

class TestWithRetryRetryable:
    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self):
        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RetryableError("transient")
            return "recovered"

        events, result = await _consume(with_retry(
            op,
            is_retryable=_is_retryable,
            extract_retry_after=_extract_retry_after,
            max_retries=3,
            label="test",
        ))
        assert result == "recovered"
        assert calls == 3
        assert len(events) == 2  # 2 retries before success

    @pytest.mark.asyncio
    async def test_exhausts_retries_raises_last_error(self):
        async def op():
            raise RetryableError("always fails")

        with pytest.raises(RetryableError, match="always fails"):
            await _consume(with_retry(
                op,
                is_retryable=_is_retryable,
                extract_retry_after=_extract_retry_after,
                max_retries=2,
                label="test",
            ))

    @pytest.mark.asyncio
    async def test_retry_events_have_correct_fields(self):
        count = 0

        async def op():
            nonlocal count
            count += 1
            if count <= 2:
                raise RetryableError("fail")
            return "ok"

        events, result = await _consume(with_retry(
            op,
            is_retryable=_is_retryable,
            extract_retry_after=_extract_retry_after,
            max_retries=3,
            label="test",
        ))
        assert result == "ok"
        assert len(events) == 2
        assert events[0].attempt == 1
        assert events[0].max_attempts == 4  # max_retries + 1
        assert events[0].label == "test"
        assert isinstance(events[0].error, RetryableError)
        assert events[1].attempt == 2
        assert events[1].max_attempts == 4


# ---------------------------------------------------------------------------
# with_retry — non-retryable errors
# ---------------------------------------------------------------------------

class TestWithRetryNonRetryable:
    @pytest.mark.asyncio
    async def test_raises_immediately(self):
        async def op():
            raise NonRetryableError("fatal")

        with pytest.raises(NonRetryableError, match="fatal"):
            await _consume(with_retry(
                op,
                is_retryable=_is_retryable,
                extract_retry_after=_extract_retry_after,
                max_retries=5,
                label="test",
            ))

    @pytest.mark.asyncio
    async def test_no_retry_events_on_non_retryable(self):
        events_collected: list[RetryEvent] = []

        async def op():
            raise NonRetryableError("fatal")

        try:
            async for item in with_retry(
                op,
                is_retryable=_is_retryable,
                extract_retry_after=_extract_retry_after,
                max_retries=3,
                label="test",
            ):
                if isinstance(item, RetryEvent):
                    events_collected.append(item)
        except NonRetryableError:
            pass

        assert len(events_collected) == 0


# ---------------------------------------------------------------------------
# with_retry — connection errors (always retried)
# ---------------------------------------------------------------------------

class TestWithRetryConnectionError:
    @pytest.mark.asyncio
    async def test_connection_error_always_retried(self):
        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            if calls < 2:
                raise ConnectionError_("network down")
            return "reconnected"

        events, result = await _consume(with_retry(
            op,
            is_retryable=_is_retryable,
            extract_retry_after=_extract_retry_after,
            connection_error_types=(ConnectionError_,),
            max_retries=3,
            label="test",
        ))
        assert result == "reconnected"
        assert calls == 2
        assert len(events) == 1  # one retry event

    @pytest.mark.asyncio
    async def test_connection_error_exhausts_retries(self):
        async def op():
            raise ConnectionError_("permanent")

        with pytest.raises(ConnectionError_, match="permanent"):
            await _consume(with_retry(
                op,
                is_retryable=_is_retryable,
                extract_retry_after=_extract_retry_after,
                connection_error_types=(ConnectionError_,),
                max_retries=1,
                label="test",
            ))


# ---------------------------------------------------------------------------
# with_retry — max_retries=0 (single attempt only)
# ---------------------------------------------------------------------------

class TestWithRetryZeroRetries:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_try(self):
        async def op():
            return "one-shot"

        events, result = await _consume(with_retry(
            op,
            is_retryable=_is_retryable,
            extract_retry_after=_extract_retry_after,
            max_retries=0,
            label="test",
        ))
        assert result == "one-shot"
        assert events == []

    @pytest.mark.asyncio
    async def test_fails_immediately_on_error(self):
        async def op():
            raise RetryableError("no retries")

        with pytest.raises(RetryableError, match="no retries"):
            await _consume(with_retry(
                op,
                is_retryable=_is_retryable,
                extract_retry_after=_extract_retry_after,
                max_retries=0,
                label="test",
            ))


# ---------------------------------------------------------------------------
# with_retry — retry-after header
# ---------------------------------------------------------------------------

class TestWithRetryAfter:
    @pytest.mark.asyncio
    async def test_uses_retry_after_from_error(self):
        count = 0

        async def op():
            nonlocal count
            count += 1
            if count == 1:
                err = RetryableError("rate limited")
                err.retry_after = 0.01  # type: ignore[attr-defined]
                raise err
            return "ok"

        events, result = await _consume(with_retry(
            op,
            is_retryable=_is_retryable,
            extract_retry_after=_extract_retry_after,
            max_retries=2,
            label="test",
        ))
        assert result == "ok"
        assert len(events) == 1
        assert events[0].delay == 0.01
