"""Anthropic provider adapter — native Claude API backend.

Corresponds to Claude Code's:
  - src/services/api/client.ts    → getAnthropicClient()
  - src/services/api/withRetry.ts → withRetry() retry logic
  - src/services/api/claude.ts    → queryModel() / queryModelWithoutStreaming()

This is the **passthrough adapter** — we use anthropic SDK directly and
no message/tool format conversion is needed. Future providers (OpenAI-compat,
Google, ...) will sit next to this in sibling packages and do the translation.

Retry behaviour:
  - Exponential back-off with jitter on 429 / 529 / 5xx / connection errors
  - Respects retry-after header when present
  - Non-retryable errors (400 bad request, 401 auth) are raised immediately
"""

from __future__ import annotations

import time
import random
import logging
from typing import ContextManager

import anthropic
from anthropic import APIError, APIConnectionError

from src import config
from src.providers.base import RetryCallback

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — match withRetry.ts
# ---------------------------------------------------------------------------
DEFAULT_MAX_RETRIES = 3
BASE_DELAY_MS = 500
MAX_DELAY_MS = 32_000


# ---------------------------------------------------------------------------
# Retry helpers (module-private; reused across the three entry points)
# ---------------------------------------------------------------------------

def _get_retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """Exponential back-off with jitter + optional retry-after header.

    Mirrors withRetry.ts getRetryDelay().
    Returns delay in seconds.
    """
    if retry_after is not None:
        return retry_after

    base_delay_s = min(
        (BASE_DELAY_MS / 1000) * (2 ** (attempt - 1)),
        MAX_DELAY_MS / 1000,
    )
    jitter = random.random() * 0.25 * base_delay_s
    return base_delay_s + jitter


def _is_retryable(error: APIError) -> bool:
    """Decide whether an API error warrants a retry."""
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status is None:
        return False
    if status == 429 or status == 529:
        return True
    if status == 408 or status == 409:
        return True
    if status >= 500:
        return True
    return False


def _extract_retry_after(error: APIError) -> float | None:
    """Pull Retry-After header (seconds) from an API error, if present."""
    headers = getattr(error, "headers", None)
    if headers is None:
        return None

    retry_after = None
    if hasattr(headers, "get"):
        retry_after = headers.get("retry-after")
    elif isinstance(headers, dict):
        retry_after = headers.get("retry-after") or headers.get("Retry-After")

    if retry_after is not None:
        try:
            return float(retry_after)
        except (ValueError, TypeError):
            pass
    return None


# ---------------------------------------------------------------------------
# AnthropicAdapter — implements ProviderAdapter Protocol
# ---------------------------------------------------------------------------

class AnthropicAdapter:
    """Native Claude API adapter (passthrough, no format translation).

    Owns its own anthropic.Anthropic client singleton so multiple
    providers can coexist without stepping on each other.
    """

    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None

    # -- client lifecycle ---------------------------------------------------

    def get_client(self) -> anthropic.Anthropic:
        """Return a reusable Anthropic client, creating it on first call.

        We set max_retries=0 because we handle retries ourselves (matching
        Claude Code's pattern).
        """
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=config.ANTHROPIC_AUTH_TOKEN,
                base_url=config.ANTHROPIC_BASE_URL,
                max_retries=0,
            )
        return self._client

    def reset_client(self) -> None:
        """Force re-creation on next call (e.g. after auth refresh)."""
        self._client = None

    # -- non-streaming ------------------------------------------------------

    def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        on_retry: RetryCallback | None = None,
    ) -> anthropic.types.Message:
        """Call messages.create() with retry and error handling.

        Mirrors the pattern in claude.ts queryModel() → withRetry().
        Raises anthropic.APIError (or subclass) on non-retryable failures.
        """
        client = self.get_client()
        resolved_model = model or config.MODEL
        resolved_max_tokens = max_tokens or config.MAX_TOKENS

        last_error: Exception | None = None

        for attempt in range(1, max_retries + 2):  # +2: attempt 1 is initial try
            try:
                params = dict(
                    model=resolved_model,
                    max_tokens=resolved_max_tokens,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
                if thinking is not None:
                    params["thinking"] = thinking
                return client.messages.create(**params)

            except APIConnectionError as e:
                last_error = e
                logger.warning(
                    "API connection error (attempt %d/%d): %s",
                    attempt, max_retries + 1, e,
                )

            except APIError as e:
                last_error = e
                status = getattr(e, "status_code", None) or getattr(e, "status", None)
                logger.warning(
                    "API error %s (attempt %d/%d): %s",
                    status, attempt, max_retries + 1, e,
                )
                if not _is_retryable(e):
                    raise

            except Exception:
                raise

            # --- Should we retry? ---
            if attempt > max_retries:
                break

            retry_after = None
            if isinstance(last_error, APIError):
                retry_after = _extract_retry_after(last_error)

            delay = _get_retry_delay(attempt, retry_after)
            logger.info(
                "Retrying in %.1fs (attempt %d/%d)...",
                delay, attempt, max_retries + 1,
            )
            if on_retry is not None:
                on_retry(delay, attempt, max_retries + 1)
            time.sleep(delay)

        assert last_error is not None
        raise last_error

    # -- streaming ----------------------------------------------------------

    def stream_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        on_retry: RetryCallback | None = None,
    ) -> ContextManager:
        """Create a streaming API call with retry, returning the stream context manager.

        The returned context manager, once entered, exposes:
          - .text_stream        : iterator of string deltas
          - .get_final_message(): final anthropic.types.Message
        """
        client = self.get_client()
        resolved_model = model or config.MODEL
        resolved_max_tokens = max_tokens or config.MAX_TOKENS

        last_error: Exception | None = None

        for attempt in range(1, max_retries + 2):
            try:
                params = dict(
                    model=resolved_model,
                    max_tokens=resolved_max_tokens,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
                if thinking is not None:
                    params["thinking"] = thinking
                return client.messages.stream(**params)

            except APIConnectionError as e:
                last_error = e
                logger.warning(
                    "API connection error (attempt %d/%d): %s",
                    attempt, max_retries + 1, e,
                )

            except APIError as e:
                last_error = e
                status = getattr(e, "status_code", None) or getattr(e, "status", None)
                logger.warning(
                    "API error %s (attempt %d/%d): %s",
                    status, attempt, max_retries + 1, e,
                )
                if not _is_retryable(e):
                    raise

            except Exception:
                raise

            if attempt > max_retries:
                break

            retry_after = None
            if isinstance(last_error, APIError):
                retry_after = _extract_retry_after(last_error)

            delay = _get_retry_delay(attempt, retry_after)
            logger.info(
                "Retrying in %.1fs (attempt %d/%d)...",
                delay, attempt, max_retries + 1,
            )
            if on_retry is not None:
                on_retry(delay, attempt, max_retries + 1)
            time.sleep(delay)

        assert last_error is not None
        raise last_error

    # -- side query ---------------------------------------------------------

    def side_query(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        max_tokens: int = 256,
        output_format: dict | None = None,
    ) -> anthropic.types.Message:
        """Lightweight LLM call for side tasks (memory recall, classification, etc.).

        No tools, no thinking, no streaming, no retry events.
        Corresponds to Claude Code's sideQuery.ts.
        """
        client = self.get_client()
        last_error: Exception | None = None

        for attempt in range(1, DEFAULT_MAX_RETRIES + 2):
            try:
                params: dict = dict(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                )
                if output_format is not None:
                    params["response_format"] = output_format
                return client.messages.create(**params)

            except APIConnectionError as e:
                last_error = e
                logger.warning("side_query connection error (attempt %d): %s", attempt, e)

            except APIError as e:
                last_error = e
                if not _is_retryable(e):
                    raise

            except Exception:
                raise

            if attempt > DEFAULT_MAX_RETRIES:
                break

            delay = _get_retry_delay(attempt)
            time.sleep(delay)

        assert last_error is not None
        raise last_error
