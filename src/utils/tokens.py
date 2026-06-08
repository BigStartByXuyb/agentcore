"""Token estimation utilities."""

from __future__ import annotations

from src.core import config
from src.core.types import MessageHistory, AgentState


def estimate_token_count(history: MessageHistory, state: AgentState | None = None) -> int:
    """Estimate current token count using hybrid approach.

    If state has last_usage_tokens (from previous API call), uses that as base
    and only estimates tokens for messages added since. Otherwise falls back to
    full local estimation.

    Uses input_tokens + output_tokens + cache_tokens as base,
    then estimates new messages at ~chars/4.
    """
    if state and state.last_usage_tokens > 0 and state.messages_since_last_usage > 0:
        new_messages = history.messages[-state.messages_since_last_usage:]
        new_estimate = rough_estimate_messages(new_messages)
        return state.last_usage_tokens + new_estimate

    return rough_estimate_messages(history.messages)


def rough_estimate_messages(messages: list) -> int:
    """Estimate token count for a list of messages using UTF-8 byte length."""
    total = 0
    for msg in messages:
        if isinstance(msg.content, str):
            total += config.get().estimate_tokens(msg.content)
        elif isinstance(msg.content, list):
            for block in msg.content:
                text = getattr(block, "text", None) or getattr(block, "content", None) or ""
                if isinstance(text, str):
                    total += config.get().estimate_tokens(text)
        for att in getattr(msg, "attachments", None) or []:
            total += config.get().estimate_tokens(att.content)
    return total
