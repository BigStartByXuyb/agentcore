"""Provider adapter protocol — the abstraction above all LLM backends.

Every concrete provider (Anthropic, DeepSeek, ...) implements this Protocol.
The api.py dispatcher + agent_loop only see this shape; they don't know
which backend is actually responding.

Design contract (async):
  - create_message()   → awaitable, returns ProviderMessage (or duck-typed
                         equivalent like anthropic.types.Message).
  - stream_message()   → returns an *async* context manager yielding a
                         ProviderStream (see stream.py).
  - side_query()       → lightweight awaitable call for memory recall,
                         classification, etc.  No tools, no streaming,
                         no retry events.

The duck-typing contract means agent_loop never needs provider-specific
branches. Anthropic's native Message already matches ProviderMessage shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, Callable

from src.providers.stream import ProviderStream

if TYPE_CHECKING:
    from src.types import Message

# Signature: (delay_seconds, attempt, max_attempts)
RetryCallback = Callable[[float, int, int], None]


class ProviderStreamCM(Protocol):
    """Async context manager that yields a ProviderStream on enter.

    All adapters' stream_message() return an object satisfying this Protocol.
    Concrete implementations: _AsyncStreamWithRetry (Anthropic), _DeepSeekStreamWithRetry (DeepSeek).
    """

    async def __aenter__(self) -> ProviderStream: ...
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None: ...


class ProviderAdapter(Protocol):
    """Unified interface implemented by every provider backend."""

    async def create_message(
        self,
        *,
        messages: list[Message],
        system: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        max_retries: int = 3,
        on_retry: RetryCallback | None = None,
    ) -> Any:
        """Non-streaming message creation.

        Receives list[Message] from prepare_messages(). Each adapter
        converts to its own API format internally.
        """
        ...

    def stream_message(
        self,
        *,
        messages: list[Message],
        system: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        max_retries: int = 3,
        on_retry: RetryCallback | None = None,
    ) -> ProviderStreamCM:
        """Streaming message creation.

        Returns an *async* context manager yielding a ProviderStream.
        """
        ...

    async def side_query(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        max_tokens: int = 256,
        output_format: dict | None = None,
    ) -> Any:
        """Lightweight call for side tasks.

        No tools, no streaming, no retry events.
        """
        ...
