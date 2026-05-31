"""API dispatcher — routes calls to the active provider adapter (async).

Public surface (async):
  - query_model_stream(...)  → unified async generator (RetryEvent + StreamEvent + ProviderMessage)
  - query_model(...)         → non-streaming convenience wrapper (side queries)
  - get_client() / reset_client() → AsyncAnthropic client singleton access

Corresponds to Claude Code's src/services/api/claude.ts:
  query_model_stream  ≈ queryModelWithStreaming
  query_model         ≈ queryModelWithoutStreaming
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, AsyncGenerator, Union

from src.core import config
from src.core.errors import classify_api_error, create_error_text
from src.providers import get_provider
from src.providers.stream import (
    StreamEvent, StreamingFallbackEvent, NonStreamingAsStream,
)
from src.providers.types import ProviderMessage, TextBlock
from src.providers.retry import (
    with_retry,
    RetryEvent,
    DEFAULT_MAX_RETRIES,
    BASE_DELAY_MS,
    MAX_DELAY_MS,
)

if TYPE_CHECKING:
    from src.core.types import Message
    from src.providers.stream import ProviderStream

logger = logging.getLogger(__name__)

StreamYield = Union[RetryEvent, StreamEvent, StreamingFallbackEvent, ProviderMessage]


# ---------------------------------------------------------------------------
# Client access (Anthropic-specific, kept for raw SDK callers)
# ---------------------------------------------------------------------------

def get_client():
    """Return the active AsyncAnthropic client."""
    from src.providers.anthropic import get_default_adapter as _get_anthropic_adapter
    return _get_anthropic_adapter().get_client()


def reset_client() -> None:
    """Force re-creation of the Anthropic client on next call."""
    from src.providers.anthropic import get_default_adapter as _get_anthropic_adapter
    _get_anthropic_adapter().reset_client()


# ---------------------------------------------------------------------------
# Shared stream consumer — used by both streaming and fallback paths
# ---------------------------------------------------------------------------

async def _iter_stream(
    api_stream: ProviderStream,
) -> AsyncGenerator[Union[StreamEvent, ProviderMessage], None]:
    """Consume a ProviderStream, yielding StreamEvent + final ProviderMessage.

    Handles two kinds of streams uniformly:
      - Real streams (from open_stream): content_block_stop has block=None,
        so we enrich it from current_message_snapshot.
      - NonStreamingAsStream: content_block_stop already carries the block,
        so we pass it through as-is.

    The caller is responsible for closing the stream.
    """
    async for event in api_stream:
        if event.type == "content_block_stop" and event.block is None:
            snapshot = api_stream.current_message_snapshot
            block = snapshot.content[event.index]
            yield StreamEvent(
                type="content_block_stop",
                index=event.index,
                block=block,
            )
        else:
            yield event
    yield await api_stream.get_final_message()


# ---------------------------------------------------------------------------
# Unified streaming entry — agent loop uses this
# ---------------------------------------------------------------------------

async def query_model_stream(
    *,
    messages: list[Message],
    system: str,
    tools: list[dict],
    model: str | None = None,
    max_tokens: int | None = None,
    thinking: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> AsyncGenerator[StreamYield, None]:
    """Unified streaming entry point.

    Never raises — all errors are yielded as ProviderMessage(is_error=True).

    Uses a while loop with a ``streaming`` flag:
      1. First pass: open_stream (real streaming)
      2. If mid-stream failure: yield StreamingFallbackEvent, flip flag,
         continue → second pass uses create_message wrapped as
         NonStreamingAsStream (same ProviderStream interface)
      3. Both passes share _iter_stream for consumption

    The yield of StreamingFallbackEvent pauses this generator. The consumer
    (agent_loop) discards partial state, then resumes us at ``continue``.
    """
    adapter = get_provider(config.get().provider)
    api_stream: ProviderStream | None = None
    streaming = True

    try:
        # At most 2 iterations: streaming → fallback
        while True:
            try:
                # --- Open connection (streaming or non-streaming) with retry ---
                if streaming:
                    open_fn = lambda: adapter.open_stream(
                        messages=messages, system=system, tools=tools,
                        model=model, max_tokens=max_tokens, thinking=thinking,
                    )
                    label = adapter.label
                else:
                    async def _open_nonstreaming():
                        msg = await adapter.create_message(
                            messages=messages, system=system, tools=tools,
                            model=model, max_tokens=max_tokens, thinking=thinking,
                        )
                        return NonStreamingAsStream(msg)

                    open_fn = _open_nonstreaming
                    label = f"{adapter.label} (fallback)"

                async for item in with_retry(
                    open_fn,
                    is_retryable=adapter.is_retryable,
                    extract_retry_after=adapter.extract_retry_after,
                    connection_error_types=adapter.connection_error_types,
                    max_retries=max_retries,
                    label=label,
                ):
                    if isinstance(item, RetryEvent):
                        yield item
                    else:
                        api_stream = item

                assert api_stream is not None

                # --- Consume stream (same path for both types) ---
                async for event in _iter_stream(api_stream):
                    yield event
                return  # success — exit generator

            except Exception as e:
                # Clean up broken stream
                if api_stream is not None:
                    try:
                        await api_stream.close()
                    except Exception:
                        pass
                    api_stream = None

                if streaming and not config.get().disable_streaming_fallback:
                    logger.warning("Streaming failed: %s; falling back to non-streaming", e)
                    streaming = False
                    yield StreamingFallbackEvent()
                    continue  # generator pauses here; resumes at next while iteration

                raise  # propagate to outer handler

    except Exception as e:
        logger.warning("API error in query_model_stream: %s", e)
        error_code = classify_api_error(e)
        yield ProviderMessage(
            content=[TextBlock(text=create_error_text(e))],
            stop_reason="error",
            is_error=True,
            error=e,
            error_code=error_code.value,
        )

    finally:
        if api_stream is not None:
            await api_stream.close()


# ---------------------------------------------------------------------------
# Non-streaming convenience — side queries (compact, recall)
# ---------------------------------------------------------------------------

async def query_model(
    *,
    messages: list[Message],
    system: str,
    tools: list[dict],
    model: str | None = None,
    max_tokens: int | None = None,
    thinking: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
    output_format: dict | None = None,
) -> ProviderMessage:
    """Non-streaming API call — for side queries that don't need streaming events.

    Never raises — all errors are returned as ProviderMessage(is_error=True),
    matching query_model_stream's error-as-message pattern.
    """
    adapter = get_provider(config.get().provider)

    try:
        result: ProviderMessage | None = None
        async for item in with_retry(
            lambda: adapter.create_message(
                messages=messages, system=system, tools=tools,
                model=model, max_tokens=max_tokens, thinking=thinking,
                output_format=output_format,
            ),
            is_retryable=adapter.is_retryable,
            extract_retry_after=adapter.extract_retry_after,
            connection_error_types=adapter.connection_error_types,
            max_retries=max_retries,
            label=adapter.label,
        ):
            if not isinstance(item, RetryEvent):
                result = item
        assert result is not None
        return result

    except Exception as e:
        logger.warning("API error in query_model: %s", e)
        error_code = classify_api_error(e)
        return ProviderMessage(
            content=[TextBlock(text=create_error_text(e))],
            stop_reason="error",
            is_error=True,
            error=e,
            error_code=error_code.value,
        )


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "BASE_DELAY_MS",
    "MAX_DELAY_MS",
    "get_client",
    "reset_client",
    "query_model_stream",
    "query_model",
]
