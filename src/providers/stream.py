"""Unified streaming interface for all providers.

Defines StreamEvent (the normalized event shape) and ProviderStream (the
Protocol that adapter streams must implement). agent_loop.py's _stream_call
iterates ProviderStream and reads StreamEvent.type / .text / .thinking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from src.providers.types import ProviderContentBlock, ProviderMessage


@dataclass
class StreamEvent:
    """Normalized streaming event emitted by all providers.

    Matches the event types agent_loop.py already checks:
      - "text"                → event.text   (delta)
      - "thinking"            → event.thinking (delta)
      - "content_block_stop"  → event.index + event.block (completed block)
      - "content_block_delta" → ignored by agent_loop

    The ``block`` field is populated by ``query_model_stream`` (in api.py)
    for ``content_block_stop`` events, carrying the completed content block
    extracted from the stream's ``current_message_snapshot``.
    """
    type: str
    text: str = ""
    thinking: str = ""
    index: int = 0
    block: ProviderContentBlock | None = None


@dataclass
class StreamingFallbackEvent:
    """Emitted by query_model_stream when streaming fails mid-way and
    falls back to a non-streaming request. The consumer should discard
    any partial state from the failed stream before processing subsequent
    events (which come from the non-streaming response)."""
    pass


class ProviderStream(Protocol):
    """Async iterator of StreamEvent + final message accessor.

    Used inside query_model_stream after adapter.open_stream().
    """

    def __aiter__(self) -> AsyncIterator[StreamEvent]: ...

    @property
    def current_message_snapshot(self) -> ProviderMessage: ...

    async def get_final_message(self) -> ProviderMessage: ...

    async def close(self) -> None: ...


class NonStreamingAsStream:
    """Wraps a ProviderMessage as a ProviderStream-compatible async iterable.

    Converts a non-streaming API response into the same StreamEvent sequence
    that a real stream would produce, so both paths share the same consumer.
    Unlike real streams, content_block_stop events already carry the block
    (no snapshot lookup needed).
    """

    def __init__(self, message: ProviderMessage) -> None:
        self._message = message

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[StreamEvent]:
        for idx, block in enumerate(self._message.content):
            if block.type == "text":
                yield StreamEvent(type="text", text=block.text)
            elif block.type == "thinking":
                yield StreamEvent(type="thinking", thinking=block.thinking)
            yield StreamEvent(type="content_block_stop", index=idx, block=block)

    @property
    def current_message_snapshot(self) -> ProviderMessage:
        return self._message

    async def get_final_message(self) -> ProviderMessage:
        return self._message

    async def close(self) -> None:
        pass
