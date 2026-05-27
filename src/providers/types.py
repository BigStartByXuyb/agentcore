"""Provider-agnostic response types.

These dataclasses define the unified shape that all provider adapters return.
agent_loop.py reads .content / .stop_reason / .usage via duck-typing —
both anthropic.types.Message (native) and ProviderMessage match this shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class TextBlock:
    text: str = ""
    type: Literal["text"] = "text"


@dataclass
class ToolUseBlock:
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    type: Literal["tool_use"] = "tool_use"


@dataclass
class ThinkingBlock:
    thinking: str = ""
    signature: str = ""
    type: Literal["thinking"] = "thinking"


@dataclass
class RedactedThinkingBlock:
    data: str = ""
    type: Literal["redacted_thinking"] = "redacted_thinking"


ProviderContentBlock = TextBlock | ToolUseBlock | ThinkingBlock | RedactedThinkingBlock


@dataclass
class ProviderMessage:
    """Unified response returned by all provider adapters.

    Duck-typed to match what agent_loop.py reads from anthropic.types.Message:
      - .content: list of blocks with .type attribute
      - .stop_reason: "end_turn" | "tool_use" | "max_tokens"
      - .usage: object with .input_tokens, .output_tokens, etc.
    """
    content: list[ProviderContentBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)
