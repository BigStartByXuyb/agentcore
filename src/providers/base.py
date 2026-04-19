"""Provider adapter protocol — the abstraction above all LLM backends.

Every concrete provider (Anthropic, OpenAI-compat, Google, ...) implements
this Protocol. The api.py dispatcher + agent_loop only see this shape;
they don't know which backend is actually responding.

Design contract (async):
  - create_message()   → awaitable, returns an Anthropic-shaped
                         Message object (native Anthropic) or a duck-typed
                         equivalent that exposes .content / .stop_reason /
                         .usage.input_tokens / .usage.output_tokens.
  - stream_message()   → returns an *async* context manager. Inside
                         `async with`, the stream object exposes
                         .text_stream (async iterator of string deltas)
                         and .get_final_message() (awaitable).
  - side_query()       → lightweight awaitable call for memory recall,
                         classification, etc.  No tools, no streaming,
                         no retry events.

The **duck-typing contract** is what makes "everything downstream treats
responses as Anthropic format" work.  Non-Anthropic adapters fake the
shape internally — agent_loop never needs provider-specific branches.
"""

from __future__ import annotations

from typing import AsyncContextManager, Protocol, Callable

import anthropic.types


# Signature: (delay_seconds, attempt, max_attempts)
RetryCallback = Callable[[float, int, int], None]


class ProviderAdapter(Protocol):
    """Unified interface implemented by every provider backend."""

    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        max_retries: int = 3,
        on_retry: RetryCallback | None = None,
    ) -> anthropic.types.Message:
        """Non-streaming message creation.

        Returns an anthropic.types.Message (native) or a duck-typed
        equivalent. All callers read .content / .stop_reason / .usage
        via the Anthropic shape.
        """
        ...

    def stream_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        max_retries: int = 3,
        on_retry: RetryCallback | None = None,
    ) -> AsyncContextManager:
        """Streaming message creation.

        Returns an *async* context manager. Inside `async with`, the
        yielded object exposes:
          - .text_stream        : async iterator of string deltas
          - .get_final_message(): awaitable returning the final
                                   Anthropic-shaped Message

        Note: the adapter may itself internally implement retry before
        yielding the stream, so the caller just awaits `__aenter__`.
        """
        ...

    async def side_query(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        max_tokens: int = 256,
        output_format: dict | None = None,
    ) -> anthropic.types.Message:
        """Lightweight call for side tasks.

        No tools, no streaming, no retry events. Used by memory recall,
        classification, summarization, etc.
        """
        ...
