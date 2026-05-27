"""Anthropic provider adapter — native Claude API backend (async).

Corresponds to Claude Code's:
  - src/services/api/client.ts    → getAnthropicClient()
  - src/services/api/claude.ts    → queryModel()

This is the native adapter — uses anthropic SDK directly with minimal
format conversion (handled by converter.py). Future providers (OpenAI-compat,
Google, ...) sit in sibling packages with their own converters.

Adapter responsibility:
  - create_message: single API call, returns ProviderMessage
  - open_stream: opens streaming connection, returns ProviderStream
  - Error classification attributes for api.py's with_retry
  No retry logic — that's handled by api.py via with_retry.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

import anthropic

if TYPE_CHECKING:
    from src.core.config import ProviderModels
from anthropic import APIConnectionError

from src.core import config
from src.providers.types import ProviderMessage
from src.providers.stream import StreamEvent
from src.providers.anthropic import converter
from src.core.types import Message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error classification (used by api.py's with_retry)
# ---------------------------------------------------------------------------

def _is_retryable(error: Exception) -> bool:
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


def _extract_retry_after(error: Exception) -> float | None:
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

def _build_thinking_dict(max_tokens: int) -> dict | None:
    """Build Anthropic-specific thinking param from config, or None if disabled."""
    if not config.THINKING_ENABLED:
        return None
    budget = min(config.THINKING_BUDGET_TOKENS, max_tokens - 1)
    if budget <= 0:
        return None
    return {"type": "enabled", "budget_tokens": budget}


class AnthropicAdapter:
    """Native Claude API adapter (passthrough, no format translation)."""

    is_retryable = staticmethod(_is_retryable)
    extract_retry_after = staticmethod(_extract_retry_after)
    connection_error_types = (APIConnectionError,)
    label = "Anthropic"

    def __init__(self) -> None:
        self._client: anthropic.AsyncAnthropic | None = None

    # -- default models -----------------------------------------------------

    def get_default_models(self) -> ProviderModels:
        from src.core.config import ProviderModels
        return ProviderModels(
            provider="anthropic",
            main="claude-sonnet-4-6",
            compact="claude-sonnet-4-6",
            side_query="claude-haiku-4-5-20251001",
            fallback="claude-haiku-4-5-20251001",
        )

    # -- client lifecycle ---------------------------------------------------

    def get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(
                api_key=config.ANTHROPIC_AUTH_TOKEN,
                base_url=config.ANTHROPIC_BASE_URL,
                max_retries=0,
            )
        return self._client

    def reset_client(self) -> None:
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
        thinking: bool = False,
        output_format: dict | None = None,
    ) -> ProviderMessage:
        """Single API call — no retry (api.py handles that)."""
        client = self.get_client()
        resolved_model = model or config.MODELS.main
        resolved_max_tokens = max_tokens or config.MAX_TOKENS
        api_messages = converter.messages_to_anthropic(messages)

        thinking_dict = _build_thinking_dict(resolved_max_tokens) if thinking else None

        params: dict[str, Any] = dict(
            model=resolved_model,
            max_tokens=resolved_max_tokens,
            system=system,
            tools=tools,
            messages=api_messages,
        )
        if thinking_dict is not None:
            params["thinking"] = thinking_dict
        if output_format is not None:
            params["response_format"] = output_format

        response = await client.messages.create(**params)
        return converter.response_to_provider(response)

    # -- streaming ----------------------------------------------------------

    async def open_stream(
        self,
        *,
        messages: list[Message],
        system: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: bool = False,
    ) -> _AnthropicStream:
        """Open a streaming connection — no retry (api.py handles that)."""
        client = self.get_client()
        resolved_model = model or config.MODELS.main
        resolved_max_tokens = max_tokens or config.MAX_TOKENS
        api_messages = converter.messages_to_anthropic(messages)

        thinking_dict = _build_thinking_dict(resolved_max_tokens) if thinking else None

        params: dict[str, Any] = dict(
            model=resolved_model,
            max_tokens=resolved_max_tokens,
            system=system,
            tools=tools,
            messages=api_messages,
        )
        if thinking_dict is not None:
            params["thinking"] = thinking_dict

        cm = client.messages.stream(**params)
        raw_stream = await cm.__aenter__()
        return _AnthropicStream(raw_stream, cm)


# ---------------------------------------------------------------------------
# _AnthropicStream — wraps SDK stream, yields StreamEvent + ProviderMessage
# ---------------------------------------------------------------------------

class _AnthropicStream:
    """Wraps Anthropic SDK's AsyncMessageStream, converting events to StreamEvent."""

    def __init__(self, raw_stream: Any, context_manager: Any = None) -> None:
        self._raw = raw_stream
        self._cm = context_manager

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
        return converter.response_to_provider(self._raw.current_message_snapshot)

    async def get_final_message(self) -> ProviderMessage:
        raw = await self._raw.get_final_message()
        return converter.response_to_provider(raw)

    async def close(self) -> None:
        if self._cm is not None:
            await self._cm.__aexit__(None, None, None)
