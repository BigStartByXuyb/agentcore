"""Layer 2: Auto Compact — LLM-based conversation summarization (Full Compact).

When token usage approaches the context window limit, calls a side LLM
to summarize the entire conversation, then replaces history with the summary.

Trigger order per API call:
  1. micro_compact (Layer 1, lossless)
  2. estimate tokens
  3. auto_compact if over threshold (Layer 2, lossy)

Corresponds to Claude Code's src/services/compact/compactHistory.ts.
"""

from __future__ import annotations

import logging

from src import config
from src.api import side_query
from src.compact.prompt import get_compact_prompt, get_compact_user_summary
from src.types import MessageHistory, AgentState

logger = logging.getLogger(__name__)

AUTOCOMPACT_BUFFER_TOKENS = 13_000


def estimate_token_count(history: MessageHistory, state: AgentState | None = None) -> int:
    """Estimate current token count using hybrid approach.

    If state has last_usage_tokens (from previous API call), uses that as base
    and only estimates tokens for messages added since. Otherwise falls back to
    full local estimation.

    Claude Code uses: input_tokens + output_tokens + cache_tokens as base,
    then estimates new messages at ~chars/4.
    """
    if state and state.last_usage_tokens > 0 and state.messages_since_last_usage > 0:
        new_messages = history.messages[-state.messages_since_last_usage:]
        new_estimate = _rough_estimate_messages(new_messages)
        return state.last_usage_tokens + new_estimate

    return _rough_estimate_messages(history.messages)


def _rough_estimate_messages(messages: list) -> int:
    """Estimate token count for a list of messages. Uses len/4 like Claude Code."""
    total = 0
    for msg in messages:
        if isinstance(msg.content, str):
            total += len(msg.content) // 4
        elif isinstance(msg.content, list):
            for block in msg.content:
                text = getattr(block, "text", None) or getattr(block, "content", None) or ""
                if isinstance(text, str):
                    total += len(text) // 4
    return total


def should_auto_compact(estimated_tokens: int) -> bool:
    """Check if estimated token count is near context window limit."""
    threshold = config.MAX_CONTEXT_WINDOW - AUTOCOMPACT_BUFFER_TOKENS
    return estimated_tokens >= threshold


async def auto_compact(history: MessageHistory) -> bool:
    """Full compact: summarize ALL messages, replace with single summary.

    Returns True if compaction was performed, False otherwise.
    """
    messages_for_api = history.normalized_for_api()
    if not messages_for_api:
        return False

    response = await side_query(
        model=config.MEMORY_SIDE_QUERY_MODEL,
        system=get_compact_prompt(),
        messages=messages_for_api,
        max_tokens=config.AUTO_COMPACT_MAX_TOKENS,
    )

    if not response or not response.content:
        logger.warning("Auto compact: LLM returned empty response")
        return False

    raw_text = ""
    for block in response.content:
        if block.type == "text":
            raw_text += block.text

    if not raw_text.strip():
        logger.warning("Auto compact: LLM returned no text content")
        return False

    summary_text = get_compact_user_summary(raw_text, suppress_follow_up=True)
    history.replace_with_summary(summary_text)

    logger.info("Auto compact: replaced %d messages with summary", len(messages_for_api))
    return True
