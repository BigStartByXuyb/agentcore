"""Anthropic format converter — bidirectional translation between internal and SDK types.

Request direction (agent_loop → Anthropic API):
  list[Message] → Anthropic API message dicts

Response direction (Anthropic API → agent_loop):
  anthropic.types.Message → ProviderMessage (unified types)

Although Anthropic is nearly a passthrough (field names map 1:1),
isolating conversion here keeps adapter.py focused on client lifecycle
and matches the structure of other providers (e.g. deepseek/converter.py).
"""

from __future__ import annotations

from typing import Any

from src.core.types import Message, _content_block_to_dict
from src.providers.types import (
    ProviderMessage,
    TextBlock,
    ToolUseBlock,
    ThinkingBlock,
    RedactedThinkingBlock,
    Usage,
)


# ---------------------------------------------------------------------------
# Request direction: list[Message] → Anthropic API dicts
# ---------------------------------------------------------------------------

def messages_to_anthropic(messages: list[Message]) -> list[dict]:
    """Convert prepared Message objects to Anthropic API format."""
    result: list[dict] = []
    for msg in messages:
        if isinstance(msg.content, str):
            api_content: str | list[dict] = msg.content
        else:
            api_content = [_content_block_to_dict(b) for b in msg.content]
        result.append({"role": msg.role, "content": api_content})
    return result


# ---------------------------------------------------------------------------
# Response direction: Anthropic SDK Message → ProviderMessage
# ---------------------------------------------------------------------------

def response_to_provider(msg: Any) -> ProviderMessage:
    """Convert an anthropic.types.Message to ProviderMessage."""
    blocks: list = []
    for block in msg.content:
        if block.type == "text":
            blocks.append(TextBlock(text=block.text))
        elif block.type == "tool_use":
            blocks.append(ToolUseBlock(id=block.id, name=block.name, input=block.input))
        elif block.type == "thinking":
            blocks.append(ThinkingBlock(thinking=block.thinking, signature=block.signature))
        elif block.type == "redacted_thinking":
            blocks.append(RedactedThinkingBlock(data=block.data))
    return ProviderMessage(
        content=blocks,
        stop_reason=msg.stop_reason or "end_turn",
        usage=Usage(
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            cache_creation_input_tokens=getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
        ),
    )
