"""DeepSeek provider adapter — OpenAI-compatible backend for DeepSeek models (async).

Implements the ProviderAdapter Protocol using the openai Python SDK pointed
at DeepSeek's API endpoint.  Handles:
  - Non-streaming (create_message) with retry
  - Streaming (stream_message) with retry on connection
  - Side query (no tools, no streaming)
  - reasoning_content → ThinkingBlock mapping (DeepSeek-R1)

All format conversion is delegated to converter.py.
"""

from __future__ import annotations

import asyncio
import json
import random
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from src.core.config import ProviderModels

from src.core import config
from src.core.types import Message
from src.providers.base import RetryCallback, ProviderStreamCM
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
# Constants — same retry policy as Anthropic adapter
# ---------------------------------------------------------------------------
DEFAULT_MAX_RETRIES = 3
BASE_DELAY_MS = 500
MAX_DELAY_MS = 32_000


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

def _get_retry_delay(attempt: int, retry_after: float | None = None) -> float:
    if retry_after is not None:
        return retry_after
    base_delay_s = min(
        (BASE_DELAY_MS / 1000) * (2 ** (attempt - 1)),
        MAX_DELAY_MS / 1000,
    )
    jitter = random.random() * 0.25 * base_delay_s
    return base_delay_s + jitter


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

    def __init__(self) -> None:
        self._client: Any = None  # openai.AsyncOpenAI

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
        max_retries: int = DEFAULT_MAX_RETRIES,
        on_retry: RetryCallback | None = None,
        output_format: dict | None = None,
    ) -> ProviderMessage:
        from openai import APIError, APIConnectionError

        client = self.get_client()
        default_model = config.DEEPSEEK_REASONER_MODEL if thinking else config.MODELS.main
        resolved_model = model or default_model
        resolved_max_tokens = max_tokens or config.MAX_TOKENS

        oai_messages = converter.messages_to_openai(messages, system)
        oai_tools = converter.tools_to_openai(tools) if tools else None

        last_error: Exception | None = None

        for attempt in range(1, max_retries + 2):
            try:
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

            except APIConnectionError as e:
                last_error = e
                logger.warning(
                    "DeepSeek connection error (attempt %d/%d): %s",
                    attempt, max_retries + 1, e,
                )

            except APIError as e:
                last_error = e
                status = getattr(e, "status_code", None) or getattr(e, "status", None)
                logger.warning(
                    "DeepSeek API error %s (attempt %d/%d): %s",
                    status, attempt, max_retries + 1, e,
                )
                if not _is_retryable_openai(e):
                    raise

            except Exception:
                raise

            if attempt > max_retries:
                break

            retry_after = None
            if isinstance(last_error, APIError):
                retry_after = _extract_retry_after_openai(last_error)

            delay = _get_retry_delay(attempt, retry_after)
            logger.info(
                "DeepSeek retrying in %.1fs (attempt %d/%d)...",
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
        thinking: bool = False,
        max_retries: int = DEFAULT_MAX_RETRIES,
        on_retry: RetryCallback | None = None,
    ) -> ProviderStreamCM:
        if thinking and model is None:
            model = config.DEEPSEEK_REASONER_MODEL
        return _DeepSeekStreamWithRetry(
            adapter=self,
            messages=messages,
            system=system,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            max_retries=max_retries,
            on_retry=on_retry,
        )


# ---------------------------------------------------------------------------
# Streaming implementation
# ---------------------------------------------------------------------------

class _DeepSeekStream:
    """Wraps an OpenAI async stream, yielding StreamEvent and building ProviderMessage."""

    def __init__(self, raw_stream: Any) -> None:
        self._raw = raw_stream
        self._text_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._tool_calls: dict[int, dict] = {}  # index → accumulated tool call
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


class _DeepSeekStreamWithRetry:
    """Async context manager that retries stream creation, then wraps in _DeepSeekStream."""

    def __init__(
        self,
        *,
        adapter: DeepSeekAdapter,
        messages: list[Message],
        system: str,
        tools: list[dict],
        model: str | None,
        max_tokens: int | None,
        max_retries: int,
        on_retry: RetryCallback | None,
    ) -> None:
        self._adapter = adapter
        self._messages = messages
        self._system = system
        self._tools = tools
        self._model = model
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._on_retry = on_retry
        self._raw_stream: Any = None
        self._stream: _DeepSeekStream | None = None

    async def __aenter__(self) -> _DeepSeekStream:
        from openai import APIError, APIConnectionError

        client = self._adapter.get_client()
        resolved_model = self._model or config.MODELS.main
        resolved_max_tokens = self._max_tokens or config.MAX_TOKENS

        oai_messages = converter.messages_to_openai(self._messages, self._system)
        oai_tools = converter.tools_to_openai(self._tools) if self._tools else None

        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 2):
            try:
                params: dict[str, Any] = dict(
                    model=resolved_model,
                    max_tokens=resolved_max_tokens,
                    messages=oai_messages,
                    stream=True,
                )
                if oai_tools:
                    params["tools"] = oai_tools
                self._raw_stream = await client.chat.completions.create(**params)
                self._stream = _DeepSeekStream(self._raw_stream)
                return self._stream

            except APIConnectionError as e:
                last_error = e
                logger.warning(
                    "DeepSeek stream connection error (attempt %d/%d): %s",
                    attempt, self._max_retries + 1, e,
                )

            except APIError as e:
                last_error = e
                status = getattr(e, "status_code", None) or getattr(e, "status", None)
                logger.warning(
                    "DeepSeek stream API error %s (attempt %d/%d): %s",
                    status, attempt, self._max_retries + 1, e,
                )
                if not _is_retryable_openai(e):
                    raise

            if attempt > self._max_retries:
                break

            retry_after = None
            if isinstance(last_error, APIError):
                retry_after = _extract_retry_after_openai(last_error)

            delay = _get_retry_delay(attempt, retry_after)
            logger.info(
                "DeepSeek stream retrying in %.1fs (attempt %d/%d)...",
                delay, attempt, self._max_retries + 1,
            )
            if self._on_retry is not None:
                self._on_retry(delay, attempt, self._max_retries + 1)
            await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._raw_stream is not None:
            await self._raw_stream.close()
        return False
