"""Agent event types — yielded by run_agent_loop(), consumed by display handlers.

Typed dataclasses that carry just the display-relevant data, not full API messages.

The generator pattern decouples the agent loop (logic) from the terminal
(presentation).  The loop yields events; the consumer decides how to render.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class AgentEvent:
    """Base class for all agent events."""
    label: str  # "main" / "fork:skill-name" / "agent:explore"


# ---------------------------------------------------------------------------
# Text events
# ---------------------------------------------------------------------------

@dataclass
class TextDelta(AgentEvent):
    """Streaming text increment (one chunk from the stream)."""
    delta: str
    first: bool = False  # True for the first delta of a new response


@dataclass
class TextBlock(AgentEvent):
    """Complete text block (non-streaming path, e.g. sub-agents)."""
    text: str


# ---------------------------------------------------------------------------
# Thinking events
# ---------------------------------------------------------------------------

@dataclass
class ThinkingBlock(AgentEvent):
    """Complete thinking block (non-streaming path)."""
    thinking: str


@dataclass
class ThinkingDelta(AgentEvent):
    """Streaming thinking increment (one chunk from the stream)."""
    delta: str
    first: bool = False


# ---------------------------------------------------------------------------
# Tool events
# ---------------------------------------------------------------------------

@dataclass
class ToolStart(AgentEvent):
    """A tool is about to be executed."""
    tool_name: str
    tool_input: dict = field(default_factory=dict)


@dataclass
class ToolEnd(AgentEvent):
    """A tool has finished executing."""
    tool_name: str
    is_error: bool = False
    result_summary: str = ""
    display_text: str = ""


# ---------------------------------------------------------------------------
# Error / recovery events
# ---------------------------------------------------------------------------

@dataclass
class ErrorEvent(AgentEvent):
    """API error or exception."""
    error_text: str


@dataclass
class Recovery(AgentEvent):
    """Thinking-related 400 recovery attempt."""
    message: str


# ---------------------------------------------------------------------------
# Token / status events
# ---------------------------------------------------------------------------

@dataclass
class TokenUsage(AgentEvent):
    """Token usage summary at end of a turn."""
    input_tokens: int
    output_tokens: int
    thinking_tokens: int = 0


@dataclass
class RetryNotice(AgentEvent):
    """API retry notification (from api.py retry loop)."""
    delay: float
    attempt: int
    max_attempts: int


# ---------------------------------------------------------------------------
# Sub-agent lifecycle events
# ---------------------------------------------------------------------------

@dataclass
class SubAgentStart(AgentEvent):
    """A sub-agent or fork skill is starting."""
    depth: int


@dataclass
class SubAgentEnd(AgentEvent):
    """A sub-agent or fork skill has completed."""
    pass


# ---------------------------------------------------------------------------
# Permission events
# ---------------------------------------------------------------------------

@dataclass
class PermissionRequest(AgentEvent):
    """Tool needs user authorization — consumer fills the Future.

    ask_mode controls interaction style:
      - standard: show preview + [y/n/always]
      - review:   show custom_prompt + [y/n], collect feedback on rejection

    custom_prompt can be set in either mode to override the prompt text.
    """
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    future: asyncio.Future | None = None
    ask_mode: str = "standard"
    custom_prompt: str | None = None
    preview: str | None = None


@dataclass
class PermissionDenied(AgentEvent):
    """Tool was denied by a permission rule."""
    tool_name: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# User interaction events
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Compact events
# ---------------------------------------------------------------------------

@dataclass
class CompactCircuitBreaker(AgentEvent):
    """Auto-compact circuit breaker triggered after consecutive failures."""
    failures: int = 0
    message: str = ""


@dataclass
class BlockingLimitReached(AgentEvent):
    """Context window hard limit reached — cannot call API."""
    estimated_tokens: int = 0
    message: str = ""


@dataclass
class UserQuestionRequest(AgentEvent):
    """LLM is asking the user a structured question — consumer fills the Future.

    The tool executor emits this event and awaits the Future. The display
    handler presents numbered options (plus an "Other" free-text option),
    collects the user's choice, and resolves the Future with the answers dict.
    """
    questions: list[dict] = field(default_factory=list)
    future: asyncio.Future | None = None
