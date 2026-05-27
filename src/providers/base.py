"""Provider adapter protocol — the abstraction above all LLM backends.

Every concrete provider (Anthropic, DeepSeek, ...) implements this Protocol.
The api.py dispatcher + agent_loop only see this shape; they don't know
which backend is actually responding.

Design contract (async):
  - create_message()  → awaitable, returns ProviderMessage. No retry —
                        retry is handled by api.py via with_retry.
  - open_stream()     → awaitable, returns a ProviderStream. No retry.
  - Error classification attributes (is_retryable, extract_retry_after,
    connection_error_types, label) used by api.py's with_retry calls.

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


class ProviderAdapter(Protocol):
    """Unified interface implemented by every provider backend."""

    is_retryable: Callable[[Exception], bool]
    extract_retry_after: Callable[[Exception], float | None]
    connection_error_types: tuple[type[Exception], ...]
    label: str

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
        output_format: dict | None = None,
    ) -> ProviderMessage:
        """Non-streaming message creation. No retry — api.py handles that."""
        ...

    async def open_stream(
        self,
        *,
        messages: list[Message],
        system: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        thinking: bool = False,
    ) -> ProviderStream:
        """Open a streaming connection, returning a ProviderStream. No retry."""
        ...
