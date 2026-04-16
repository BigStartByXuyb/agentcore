"""API client and request wrapper — centralised Claude API access.

Corresponds to Claude Code's:
  - src/services/api/client.ts   → getAnthropicClient()
  - src/services/api/withRetry.ts → withRetry() retry logic
  - src/services/api/claude.ts   → queryModel() / queryModelWithoutStreaming()

Design:
  - get_client()       — lazily creates a reusable Anthropic client
  - query_model()      — the single call-site for messages.create(),
                         wraps it with retry + error handling
  - Retry is exponential back-off on transient errors (429, 529, 5xx,
    connection errors) with configurable max retries.
  - Non-retryable errors (400 bad request, 401 auth, etc.) are raised
    immediately so callers can react.
"""

from __future__ import annotations

import time
import random
import logging

import anthropic
from anthropic import APIError, APIConnectionError

from src import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — match withRetry.ts
# ---------------------------------------------------------------------------
DEFAULT_MAX_RETRIES = 3          # simpler default than CC's 10 (no streaming)
BASE_DELAY_MS = 500              # matches withRetry.ts BASE_DELAY_MS
MAX_DELAY_MS = 32_000            # cap exponential back-off

# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------
_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    """Return a reusable Anthropic client, creating it on first call.

    Mirrors client.ts getAnthropicClient() — we create once and reuse.
    The anthropic SDK handles connection pooling internally.
    """
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=config.ANTHROPIC_AUTH_TOKEN,
            base_url=config.ANTHROPIC_BASE_URL,
            max_retries=0,  # We handle retries ourselves (same as CC)
        )
    return _client


def reset_client() -> None:
    """Force re-creation on next call (e.g. after auth refresh)."""
    global _client
    _client = None


# ---------------------------------------------------------------------------
# Retry helpers — simplified withRetry.ts
# ---------------------------------------------------------------------------

def _get_retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """Exponential back-off with jitter + optional retry-after header.

    Mirrors withRetry.ts getRetryDelay().
    Returns delay in *seconds*.
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
    """Decide whether an API error warrants a retry.

    Mirrors withRetry.ts shouldRetry() — simplified for direct API usage.
    """
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status is None:
        return False

    # 429 rate limit — always retry
    if status == 429:
        return True
    # 529 overloaded — always retry
    if status == 529:
        return True
    # 408 request timeout
    if status == 408:
        return True
    # 409 lock timeout
    if status == 409:
        return True
    # 5xx server errors
    if status >= 500:
        return True

    return False


def _extract_retry_after(error: APIError) -> float | None:
    """Pull Retry-After header (seconds) from an API error, if present."""
    headers = getattr(error, "headers", None)
    if headers is None:
        return None

    # headers may be a dict or a httpx.Headers object
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
# query_model — the single API call-site
# ---------------------------------------------------------------------------

def query_model(
    *,
    messages: list[dict],
    system: str,
    tools: list[dict],
    model: str | None = None,
    max_tokens: int | None = None,
    thinking: dict | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    on_retry: callable | None = None,
) -> anthropic.types.Message:
    """Call Claude messages.create() with retry and error handling.

    This is the **only** place in the codebase that calls the API.
    Mirrors the pattern in claude.ts queryModel() → withRetry():
      1. Build params
      2. Loop: call API → on retryable error sleep → retry
      3. Non-retryable errors propagate immediately

    Parameters:
        on_retry:  Optional callback invoked before each retry sleep.
                   Signature: (delay: float, attempt: int, max_attempts: int) -> None.
                   Used by the agent loop to yield RetryNotice events.

    Returns the raw anthropic Message object.
    Raises anthropic.APIError (or subclass) on non-retryable failures.
    """
    client = get_client()
    resolved_model = model or config.MODEL
    resolved_max_tokens = max_tokens or config.MAX_TOKENS

    last_error: Exception | None = None

    for attempt in range(1, max_retries + 2):  # +2 because attempt 1 is the initial try
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
            response = client.messages.create(**params)
            return response

        except APIConnectionError as e:
            # Network-level failure (includes timeout) — always retry
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
                # 400 bad request, 401 unauthorized, 403 forbidden, etc.
                # No point retrying — raise immediately.
                raise

        except Exception as e:
            # Unexpected error — don't retry, propagate
            raise

        # --- Should we retry? ---
        if attempt > max_retries:
            break

        # Compute delay
        retry_after = None
        if isinstance(last_error, APIError):
            retry_after = _extract_retry_after(last_error)

        delay = _get_retry_delay(attempt, retry_after)
        logger.info("Retrying in %.1fs (attempt %d/%d)...", delay, attempt, max_retries + 1)
        if on_retry is not None:
            on_retry(delay, attempt, max_retries + 1)
        time.sleep(delay)

    # All retries exhausted
    assert last_error is not None
    raise last_error


# ---------------------------------------------------------------------------
# create_stream_with_retry — returns raw stream context manager
# ---------------------------------------------------------------------------

def create_stream_with_retry(
    *,
    messages: list[dict],
    system: str,
    tools: list[dict],
    model: str | None = None,
    max_tokens: int | None = None,
    thinking: dict | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    on_retry: callable | None = None,
):
    """Create a streaming API call with retry, returning the stream context manager.

    Unlike query_model_stream() which consumes the stream internally, this
    returns the raw MessageStreamManager so the caller can iterate
    stream.text_stream directly and yield events in real-time.

    Usage:
        stream_cm = create_stream_with_retry(messages=..., system=..., tools=...)
        with stream_cm as stream:
            for text in stream.text_stream:
                yield TextDelta(text)
            response = stream.get_final_message()
    """
    client = get_client()
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

            # Return the context manager — caller enters with `with`
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

        except Exception as e:
            raise

        # --- Should we retry? ---
        if attempt > max_retries:
            break

        retry_after = None
        if isinstance(last_error, APIError):
            retry_after = _extract_retry_after(last_error)

        delay = _get_retry_delay(attempt, retry_after)
        logger.info("Retrying in %.1fs (attempt %d/%d)...", delay, attempt, max_retries + 1)
        if on_retry is not None:
            on_retry(delay, attempt, max_retries + 1)
        time.sleep(delay)

    assert last_error is not None
    raise last_error


# ---------------------------------------------------------------------------
# side_query — lightweight LLM call for side tasks
# ---------------------------------------------------------------------------

def side_query(
    *,
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int = 256,
    output_format: dict | None = None,
) -> anthropic.types.Message:
    """Lightweight LLM call for side tasks (memory recall, classification, etc.).

    No tools, no thinking, no streaming, no retry events.
    Uses the same retry logic as query_model() but with simpler params.

    Corresponds to Claude Code's src/utils/sideQuery.ts — a generic
    utility used by memory recall, permission explainer, session search, etc.

    Parameters:
        output_format: Optional JSON schema for structured output.
                       Example: {"type": "json", "schema": {...}}
    """
    client = get_client()

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

def query_model_stream(
    *,
    messages: list[dict],
    system: str,
    tools: list[dict],
    model: str | None = None,
    max_tokens: int | None = None,
    thinking: dict | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    on_text: callable | None = None,
    on_retry: callable | None = None,
) -> anthropic.types.Message:
    """Call Claude messages.create() with streaming, retry and error handling.

    Same as query_model() but uses the streaming API so text tokens can be
    displayed incrementally via the *on_text* callback.

    Parameters:
        on_text:  Called with each text delta string as it arrives.
                  Signature: (str) -> None.  If None, text is silently
                  consumed (useful for sub-agents where we don't print).

    Returns the **final** anthropic Message object (identical structure to
    the non-streaming query_model) so callers can process tool_use blocks,
    stop_reason, and usage exactly the same way.

    Corresponds to Claude Code's streaming path in claude.ts queryModel()
    which uses messages.stream() and accumulates via MessageStream events.
    """
    client = get_client()
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

            with client.messages.stream(**params) as stream:
                # Deliver text deltas in real-time
                if on_text is not None:
                    for text in stream.text_stream:
                        on_text(text)

                # get_final_message() returns the complete Message — same
                # object shape as messages.create(), including .usage,
                # .stop_reason, .content with all text + tool_use blocks.
                return stream.get_final_message()

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

        except Exception as e:
            raise

        # --- Should we retry? ---
        if attempt > max_retries:
            break

        retry_after = None
        if isinstance(last_error, APIError):
            retry_after = _extract_retry_after(last_error)

        delay = _get_retry_delay(attempt, retry_after)
        logger.info("Retrying in %.1fs (attempt %d/%d)...", delay, attempt, max_retries + 1)
        if on_retry is not None:
            on_retry(delay, attempt, max_retries + 1)
        time.sleep(delay)

    assert last_error is not None
    raise last_error
