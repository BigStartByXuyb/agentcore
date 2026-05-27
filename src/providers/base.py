"""Provider adapter protocol — the abstraction above all LLM backends.

Every concrete provider (Anthropic, DeepSeek, ...) implements this Protocol.
The api.py dispatcher + agent_loop only see this shape; they don't know
which backend is actually responding.

Design contract (async):
  - create_message()   → awaitable, returns ProviderMessage (or duck-typed
                         equivalent like anthropic.types.Message).
                         Pass tools=[] for non-tool-using calls (e.g.
                         compact, memory recall).
  - stream_message()   → returns an *async* context manager yielding a
                         ProviderStream (see stream.py).

The duck-typing contract means agent_loop never needs provider-specific
branches. Anthropic's native Message already matches ProviderMessage shape.

Thinking parameter:
  Both methods accept `thinking: bool`. When True, each adapter
  enables its own thinking mechanism internally:
    - Anthropic: sends {"type": "enabled", "budget_tokens": ...}
    - DeepSeek: switches to deepseek-reasoner model
  Callers never build provider-specific thinking dicts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, Callable

from src.providers.stream import ProviderStream
from src.providers.types import ProviderMessage

if TYPE_CHECKING:
    from src.core.config import ProviderModels
    from src.core.types import Message

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

    def get_default_models(self) -> ProviderModels:
        """Return the provider's default model tiers."""
        ...

    async def create_message(
        self,
        *,
        messages: list[Message],
        system: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: bool = False,
        max_retries: int = 3,
        on_retry: RetryCallback | None = None,
        output_format: dict | None = None,
    ) -> ProviderMessage:
        """Non-streaming message creation."""
        ...

    def stream_message(
        self,
        *,
        messages: list[Message],
        system: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: bool = False,
        max_retries: int = 3,
        on_retry: RetryCallback | None = None,
    ) -> ProviderStreamCM:
        """Streaming message creation."""
        ...
