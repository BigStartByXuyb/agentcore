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
from src.providers.stream import StreamEvent
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

logger = logging.getLogger(__name__)

StreamYield = Union[RetryEvent, StreamEvent, ProviderMessage]


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

    Never raises — all errors are yielded as ProviderMessage(is_error=True),
    matching Claude Code's pattern where queryModel yields error messages
    instead of throwing.

    Two phases:

    Phase 1 — with_retry establishes the connection:
      yields RetryEvent for each retry attempt (real-time notification)
      yields the ProviderStream object on success

    Phase 2 — iterate the stream:
      yields StreamEvent (text/thinking deltas, content_block_stop with block)
      yields ProviderMessage as the final item (success or error)
    """
    adapter = get_provider(config.PROVIDER)
    api_stream = None

    try:
        # Phase 1: establish connection with retry
        async for item in with_retry(
            lambda: adapter.open_stream(
                messages=messages, system=system, tools=tools,
                model=model, max_tokens=max_tokens, thinking=thinking,
            ),
            is_retryable=adapter.is_retryable,
            extract_retry_after=adapter.extract_retry_after,
            connection_error_types=adapter.connection_error_types,
            max_retries=max_retries,
            label=adapter.label,
        ):
            if isinstance(item, RetryEvent):
                yield item
            else:
                api_stream = item

        assert api_stream is not None

        # Phase 2: iterate the stream
        async for event in api_stream:
            if event.type == "content_block_stop":
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
    """Non-streaming API call — for side queries that don't need streaming events."""
    adapter = get_provider(config.PROVIDER)
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


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "BASE_DELAY_MS",
    "MAX_DELAY_MS",
    "get_client",
    "reset_client",
    "query_model_stream",
    "query_model",
]
