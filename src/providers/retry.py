"""Shared retry logic for all provider adapters.

Provider-agnostic: does NOT import anthropic, openai, or any SDK.
Each adapter supplies its own error-classification callbacks.

Corresponds to Claude Code's src/services/api/withRetry.ts.

with_retry is an async generator:
  - yields RetryEvent during retry delays (real-time notification)
  - yields T (the result) as the final item on success
  - raises on non-retryable errors or when retries are exhausted
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import AsyncGenerator, Awaitable, Callable, TypeVar, Union

T = TypeVar("T")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (previously duplicated in each adapter)
# ---------------------------------------------------------------------------
DEFAULT_MAX_RETRIES = 3
BASE_DELAY_MS = 500
MAX_DELAY_MS = 32_000


# ---------------------------------------------------------------------------
# RetryEvent — yielded by with_retry during retries
# ---------------------------------------------------------------------------

@dataclass
class RetryEvent:
    """Emitted by with_retry before each retry sleep."""
    delay: float
    attempt: int
    max_attempts: int
    error: Exception
    label: str


# ---------------------------------------------------------------------------
# Delay calculation
# ---------------------------------------------------------------------------

def get_retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """Exponential back-off with jitter + optional retry-after override.

    Returns delay in **seconds**.
    """
    if retry_after is not None:
        return retry_after

    base_delay_s = min(
        (BASE_DELAY_MS / 1000) * (2 ** (attempt - 1)),
        MAX_DELAY_MS / 1000,
    )
    jitter = random.random() * 0.25 * base_delay_s
    return base_delay_s + jitter


# ---------------------------------------------------------------------------
# Core retry wrapper — async generator
# ---------------------------------------------------------------------------

async def with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    is_retryable: Callable[[Exception], bool],
    extract_retry_after: Callable[[Exception], float | None],
    connection_error_types: tuple[type[Exception], ...] = (),
    max_retries: int = DEFAULT_MAX_RETRIES,
    label: str = "API",
) -> AsyncGenerator[Union[RetryEvent, T], None]:
    """Execute *operation* with automatic retry on transient errors.

    Async generator that:
      - yields RetryEvent before each retry sleep (real-time notification)
      - yields T (the operation result) as the final item on success
      - raises on non-retryable errors or when retries are exhausted

    Consumers iterate with ``async for item in with_retry(...):`` and use
    ``isinstance(item, RetryEvent)`` to distinguish retry events from the
    final result.
    """
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 2):
        try:
            result = await operation()
            yield result
            return

        except connection_error_types as e:
            last_error = e
            logger.warning(
                "%s connection error (attempt %d/%d): %s",
                label, attempt, max_retries + 1, e,
            )

        except Exception as e:
            last_error = e
            if not is_retryable(e):
                raise
            logger.warning(
                "%s error (attempt %d/%d): %s",
                label, attempt, max_retries + 1, e,
            )

        if attempt > max_retries:
            break

        retry_after: float | None = None
        if last_error is not None:
            retry_after = extract_retry_after(last_error)

        delay = get_retry_delay(attempt, retry_after)
        logger.info(
            "%s retrying in %.1fs (attempt %d/%d)...",
            label, delay, attempt, max_retries + 1,
        )
        yield RetryEvent(
            delay=delay,
            attempt=attempt,
            max_attempts=max_retries + 1,
            error=last_error,
            label=label,
        )
        await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error
