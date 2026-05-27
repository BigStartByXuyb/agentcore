"""DeepSeek provider adapter — OpenAI-compatible backend for DeepSeek models (async).

Implements the ProviderAdapter Protocol using the openai Python SDK pointed
at DeepSeek's API endpoint.  Handles:
  - Non-streaming (create_message) — single API call
  - Streaming (open_stream) — opens connection, returns ProviderStream
  - reasoning_content → ThinkingBlock mapping (DeepSeek-R1)

No retry logic — that's handled by api.py via with_retry.
All format conversion is delegated to converter.py.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from src.core.config import ProviderModels

from src.core import config
from src.core.types import Message
from src.providers.types import (
    ProviderMessage,
    TextBlock,
    ToolUseBlock,
    ThinkingBlock,
    Usage,
)
from src.providers.stream import StreamEvent
from src.providers.deepseek import converter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error classification (used by api.py's with_retry)
# ---------------------------------------------------------------------------

def _is_retryable_openai(error: Any) -> bool:
    from openai import APIError
    if not isinstance(error, APIError):
        return False
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status is None:
        return False
    return status in (429, 529, 408, 409) or status >= 500


def _extract_retry_after_openai(error: Any) -> float | None:
    headers = getattr(error, "headers", None) or getattr(error, "response_headers", None)
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
# DeepSeekAdapter — implements ProviderAdapter Protocol
# ---------------------------------------------------------------------------

class DeepSeekAdapter:
    """DeepSeek API adapter using the openai SDK."""

    is_retryable = staticmethod(_is_retryable_openai)
    extract_retry_after = staticmethod(_extract_retry_after_openai)
    label = "DeepSeek"

    def __init__(self) -> None:
        self._client: Any = None  # openai.AsyncOpenAI
        self._connection_error_types: tuple[type[Exception], ...] | None = None

    @property
    def connection_error_types(self) -> tuple[type[Exception], ...]:
        if self._connection_error_types is None:
            from openai import APIConnectionError
            self._connection_error_types = (APIConnectionError,)
        return self._connection_error_types

    # -- default models -----------------------------------------------------

    def get_default_models(self) -> ProviderModels:
        from src.core.config import ProviderModels
        return ProviderModels(
            provider="deepseek",
            main="deepseek-chat",
            compact="deepseek-chat",
            side_query="deepseek-chat",
            fallback="deepseek-chat",
        )

    def get_client(self) -> Any:
        if self._client is None:
            import openai
            self._client = openai.AsyncOpenAI(
                api_key=config.DEEPSEEK_API_KEY,
                base_url=config.DEEPSEEK_BASE_URL or "https://api.deepseek.com",
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
        default_model = config.DEEPSEEK_REASONER_MODEL if thinking else config.MODELS.main
        resolved_model = model or default_model
        resolved_max_tokens = max_tokens or config.MAX_TOKENS

        oai_messages = converter.messages_to_openai(messages, system)
        oai_tools = converter.tools_to_openai(tools) if tools else None

        params: dict[str, Any] = dict(
            model=resolved_model,
            max_tokens=resolved_max_tokens,
            messages=oai_messages,
        )
        if oai_tools:
            params["tools"] = oai_tools
        if output_format is not None:
            params["response_format"] = output_format

        response = await client.chat.completions.create(**params)
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
    ) -> _DeepSeekStream:
        """Open a streaming connection — no retry (api.py handles that)."""
        if thinking and model is None:
            model = config.DEEPSEEK_REASONER_MODEL

        client = self.get_client()
        resolved_model = model or config.MODELS.main
        resolved_max_tokens = max_tokens or config.MAX_TOKENS

        oai_messages = converter.messages_to_openai(messages, system)
        oai_tools = converter.tools_to_openai(tools) if tools else None

        params: dict[str, Any] = dict(
            model=resolved_model,
            max_tokens=resolved_max_tokens,
            messages=oai_messages,
            stream=True,
        )
        if oai_tools:
            params["tools"] = oai_tools

        raw_stream = await client.chat.completions.create(**params)
        return _DeepSeekStream(raw_stream)


# ---------------------------------------------------------------------------
# Streaming implementation
# ---------------------------------------------------------------------------

class _DeepSeekStream:
    """Wraps an OpenAI async stream, yielding StreamEvent and building ProviderMessage."""

    def __init__(self, raw_stream: Any) -> None:
        self._raw = raw_stream
        self._text_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._tool_calls: dict[int, dict] = {}
        self._finish_reason: str | None = None
        self._usage: Usage = Usage()
        self._final_snapshot: ProviderMessage | None = None

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[StreamEvent]:
        async for chunk in self._raw:
            if not chunk.choices:
                if chunk.usage:
                    self._usage = Usage(
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                    )
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if choice.finish_reason:
                self._finish_reason = choice.finish_reason

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                self._reasoning_parts.append(reasoning)
                yield StreamEvent(type="thinking", thinking=reasoning)

            if delta.content:
                self._text_parts.append(delta.content)
                yield StreamEvent(type="text", text=delta.content)

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in self._tool_calls:
                        self._tool_calls[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    entry = self._tool_calls[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["arguments"] += tc_delta.function.arguments

        self._final_snapshot = self._build_message()
        for i, block in enumerate(self._final_snapshot.content):
            if block.type == "tool_use":
                yield StreamEvent(type="content_block_stop", index=i)

    @property
    def current_message_snapshot(self) -> ProviderMessage:
        if self._final_snapshot is not None:
            return self._final_snapshot
        return self._build_message()

    async def get_final_message(self) -> ProviderMessage:
        if self._final_snapshot is not None:
            return self._final_snapshot
        return self._build_message()

    async def close(self) -> None:
        if self._raw is not None:
            await self._raw.close()

    def _build_message(self) -> ProviderMessage:
        blocks: list = []

        if self._reasoning_parts:
            blocks.append(ThinkingBlock(
                thinking="".join(self._reasoning_parts),
                signature="",
            ))

        text = "".join(self._text_parts)
        if text:
            blocks.append(TextBlock(text=text))

        for idx in sorted(self._tool_calls.keys()):
            tc = self._tool_calls[idx]
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = {"_raw": tc["arguments"]}
            blocks.append(ToolUseBlock(
                id=tc["id"],
                name=tc["name"],
                input=args,
            ))

        stop_reason = converter._map_finish_reason(self._finish_reason)

        return ProviderMessage(
            content=blocks,
            stop_reason=stop_reason,
            usage=self._usage,
        )
