"""Anthropic provider adapter — native Claude API backend (async).

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

Async model:
  - create_message / side_query: `await adapter.method(...)`
  - stream_message: returns an async context manager.  Use
      `async with adapter.stream_message(...) as stream:
           async for text in stream.text_stream: ...
           final = await stream.get_final_message()`
    Retry is handled internally on `__aenter__` so the user just awaits
    entering the context.
"""

from __future__ import annotations

import asyncio
import random
import logging
from typing import Any, AsyncIterator

import anthropic
from anthropic import APIError, APIConnectionError

from src import config
from src.providers.base import RetryCallback, ProviderStreamCM
from src.providers.types import (
    ProviderMessage,
    TextBlock,
    ToolUseBlock,
    ThinkingBlock,
    RedactedThinkingBlock,
    Usage,
)
from src.providers.stream import StreamEvent
from src.types import (
    Message,
    ContentBlock,
    TextContent,
    ToolUseContent,
    ToolResultContent,
    ThinkingContent,
    RedactedThinkingContent,
    _content_block_to_dict,
)

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
# Message format conversion: list[Message] → Anthropic API dicts
# ---------------------------------------------------------------------------

def _messages_to_anthropic(messages: list[Message]) -> list[dict]:
    """Convert prepared Message objects to Anthropic API format.

    Each Message's content (str or list[ContentBlock]) is converted to
    the plain dict format the Anthropic SDK expects.
    """
    result: list[dict] = []
    for msg in messages:
        if isinstance(msg.content, str):
            api_content: str | list[dict] = msg.content
        else:
            api_content = [_content_block_to_dict(b) for b in msg.content]
        result.append({"role": msg.role, "content": api_content})
    return result


# ---------------------------------------------------------------------------
# AnthropicAdapter — implements ProviderAdapter Protocol
# ---------------------------------------------------------------------------

class AnthropicAdapter:
    """Native Claude API adapter (passthrough, no format translation).

    Owns its own anthropic.AsyncAnthropic client singleton so multiple
    providers can coexist without stepping on each other.
    """

    def __init__(self) -> None:
        self._client: anthropic.AsyncAnthropic | None = None

    # -- client lifecycle ---------------------------------------------------

    def get_client(self) -> anthropic.AsyncAnthropic:
        """Return a reusable AsyncAnthropic client, creating it on first call.

        We set max_retries=0 because we handle retries ourselves (matching
        Claude Code's pattern).
        """
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(
                api_key=config.ANTHROPIC_AUTH_TOKEN,
                base_url=config.ANTHROPIC_BASE_URL,
                max_retries=0,
            )
        return self._client

    def reset_client(self) -> None:
        """Force re-creation on next call (e.g. after auth refresh)."""
        self._client = None

    # -- non-streaming ------------------------------------------------------

    async def create_message(
        self,
        *,
        messages: list[Message],
        system: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        on_retry: RetryCallback | None = None,
    ) -> anthropic.types.Message:
        """Call messages.create() with retry and error handling."""
        client = self.get_client()
        resolved_model = model or config.MODEL
        resolved_max_tokens = max_tokens or config.MAX_TOKENS
        api_messages = _messages_to_anthropic(messages)

        last_error: Exception | None = None

        for attempt in range(1, max_retries + 2):  # +2: attempt 1 is initial try
            try:
                params: dict[str, Any] = dict(
                    model=resolved_model,
                    max_tokens=resolved_max_tokens,
                    system=system,
                    tools=tools,
                    messages=api_messages,
                )
                if thinking is not None:
                    params["thinking"] = thinking
                return await client.messages.create(**params)

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
            await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    # -- streaming ----------------------------------------------------------

    def stream_message(
        self,
        *,
        messages: list[Message],
        system: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        on_retry: RetryCallback | None = None,
    ) -> ProviderStreamCM:
        """Create a streaming API call with retry, returning an *async* stream CM."""
        client = self.get_client()
        resolved_model = model or config.MODEL
        resolved_max_tokens = max_tokens or config.MAX_TOKENS
        api_messages = _messages_to_anthropic(messages)

        params: dict[str, Any] = dict(
            model=resolved_model,
            max_tokens=resolved_max_tokens,
            system=system,
            tools=tools,
            messages=api_messages,
        )
        if thinking is not None:
            params["thinking"] = thinking

        return _AsyncStreamWithRetry(
            client=client,
            params=params,
            max_retries=max_retries,
            on_retry=on_retry,
        )

    # -- side query ---------------------------------------------------------

    async def side_query(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        max_tokens: int = 256,
        output_format: dict | None = None,
    ) -> anthropic.types.Message:
        """Lightweight LLM call for side tasks."""
        client = self.get_client()
        api_messages = _messages_to_anthropic(messages)
        last_error: Exception | None = None

        for attempt in range(1, DEFAULT_MAX_RETRIES + 2):
            try:
                params: dict[str, Any] = dict(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=api_messages,
                )
                if output_format is not None:
                    params["response_format"] = output_format
                return await client.messages.create(**params)

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
            await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error


# ---------------------------------------------------------------------------
# _AnthropicStream — wraps SDK stream, yields StreamEvent + ProviderMessage
# ---------------------------------------------------------------------------

def _sdk_message_to_provider(msg: Any) -> ProviderMessage:
    """Convert an anthropic.types.Message to ProviderMessage."""
    blocks: list = []
    for block in msg.content:
        if block.type == "text":
            blocks.append(TextBlock(text=block.text))
        elif block.type == "tool_use":
            blocks.append(ToolUseBlock(id=block.id, name=block.name, input=block.input))
        elif block.type == "thinking":
            blocks.append(ThinkingBlock(thinking=block.thinking, signature=block.signature))
        elif block.type == "redacted_thinking":
            blocks.append(RedactedThinkingBlock(data=block.data))
    return ProviderMessage(
        content=blocks,
        stop_reason=msg.stop_reason or "end_turn",
        usage=Usage(
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            cache_creation_input_tokens=getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
        ),
    )


class _AnthropicStream:
    """Wraps Anthropic SDK's AsyncMessageStream, converting events to StreamEvent."""

    def __init__(self, raw_stream: Any) -> None:
        self._raw = raw_stream

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[StreamEvent]:
        async for event in self._raw:
            if event.type == "text":
                yield StreamEvent(type="text", text=event.text)
            elif event.type == "thinking":
                yield StreamEvent(type="thinking", thinking=event.thinking)
            elif event.type == "content_block_delta":
                yield StreamEvent(type="content_block_delta")
            elif event.type == "content_block_stop":
                yield StreamEvent(type="content_block_stop", index=event.index)

    @property
    def current_message_snapshot(self) -> ProviderMessage:
        return _sdk_message_to_provider(self._raw.current_message_snapshot)

    async def get_final_message(self) -> ProviderMessage:
        raw = await self._raw.get_final_message()
        return _sdk_message_to_provider(raw)


# ---------------------------------------------------------------------------
# _AsyncStreamWithRetry — async context manager wrapping stream creation
# ---------------------------------------------------------------------------

class _AsyncStreamWithRetry:
    """Async CM that retries stream open, then proxies to the live stream."""

    def __init__(
        self,
        *,
        client: anthropic.AsyncAnthropic,
        params: dict,
        max_retries: int,
        on_retry: RetryCallback | None,
    ) -> None:
        self._client = client
        self._params = params
        self._max_retries = max_retries
        self._on_retry = on_retry
        self._inner_cm = None
        self._stream = None

    async def __aenter__(self):
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 2):
            try:
                cm = self._client.messages.stream(**self._params)
                raw_stream = await cm.__aenter__()
                self._inner_cm = cm
                self._stream = _AnthropicStream(raw_stream)
                return self._stream

            except APIConnectionError as e:
                last_error = e
                logger.warning(
                    "API connection error (attempt %d/%d): %s",
                    attempt, self._max_retries + 1, e,
                )

            except APIError as e:
                last_error = e
                status = getattr(e, "status_code", None) or getattr(e, "status", None)
                logger.warning(
                    "API error %s (attempt %d/%d): %s",
                    status, attempt, self._max_retries + 1, e,
                )
                if not _is_retryable(e):
                    raise

            if attempt > self._max_retries:
                break

            retry_after = None
            if isinstance(last_error, APIError):
                retry_after = _extract_retry_after(last_error)

            delay = _get_retry_delay(attempt, retry_after)
            logger.info(
                "Retrying in %.1fs (attempt %d/%d)...",
                delay, attempt, self._max_retries + 1,
            )
            if self._on_retry is not None:
                self._on_retry(delay, attempt, self._max_retries + 1)
            await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    async def __aexit__(self, exc_type, exc, tb):
        if self._inner_cm is not None:
            return await self._inner_cm.__aexit__(exc_type, exc, tb)
        return False
