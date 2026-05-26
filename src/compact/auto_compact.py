"""Layer 2: Auto Compact — LLM-based conversation summarization (Full Compact).

When token usage approaches the context window limit, calls a side LLM
to summarize the entire conversation, then replaces history with the summary.

Trigger order per API call:
  1. micro_compact (Layer 1, lossless)
  2. estimate tokens
  3. auto_compact if over threshold (Layer 2, lossy)

Includes truncate-head retry for when the compaction LLM call itself
exceeds the context window (prompt_too_long on side_query).

Corresponds to Claude Code's src/services/compact/compactHistory.ts.
"""

from __future__ import annotations

import logging

from src import config
from src.api import side_query
from src.compact.prompt import get_compact_prompt, get_compact_user_summary
from src.compact.grouping import truncate_head, MAX_PTL_RETRIES
from src.types import MessageHistory, Message, AgentState

logger = logging.getLogger(__name__)

AUTOCOMPACT_BUFFER_TOKENS = 13_000
MAX_CONSECUTIVE_COMPACT_FAILURES = 3
BLOCKING_LIMIT_BUFFER_TOKENS = 3_000


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
    """Estimate token count for a list of messages using UTF-8 byte length."""
    total = 0
    for msg in messages:
        if isinstance(msg.content, str):
            total += config.estimate_tokens(msg.content)
        elif isinstance(msg.content, list):
            for block in msg.content:
                text = getattr(block, "text", None) or getattr(block, "content", None) or ""
                if isinstance(text, str):
                    total += config.estimate_tokens(text)
    return total


def should_auto_compact(estimated_tokens: int) -> bool:
    """Check if estimated token count is near context window limit."""
    threshold = config.MAX_CONTEXT_WINDOW - AUTOCOMPACT_BUFFER_TOKENS
    return estimated_tokens >= threshold


def is_at_blocking_limit(estimated_tokens: int) -> bool:
    """Check if tokens are at the hard limit — too dangerous to call API."""
    return estimated_tokens >= config.MAX_CONTEXT_WINDOW - BLOCKING_LIMIT_BUFFER_TOKENS


def _is_prompt_too_long(error: Exception) -> bool:
    """Detect prompt_too_long errors from side_query."""
    msg = str(error).lower()
    return "prompt is too long" in msg or "prompt_too_long" in msg


def _messages_to_prepared(messages: list[Message]) -> list[Message]:
    """Expand attachments + merge same-role, reusing MessageHistory's logic."""
    return MessageHistory(messages=messages).prepare_messages()


async def auto_compact(history: MessageHistory) -> bool:
    """Full compact with truncate-head retry on prompt_too_long.

    If the compaction side_query itself gets prompt_too_long, drops the
    oldest 20% of message groups and retries (up to MAX_PTL_RETRIES times).

    Returns True if compaction was performed, False otherwise.
    """
    prepared = history.prepare_messages()
    if not prepared:
        return False

    source_messages = list(history.messages)

    for attempt in range(MAX_PTL_RETRIES + 1):
        try:
            response = await side_query(
                model=config.MEMORY_SIDE_QUERY_MODEL,
                system=get_compact_prompt(),
                messages=prepared,
                max_tokens=config.AUTO_COMPACT_MAX_TOKENS,
            )
        except Exception as e:
            if _is_prompt_too_long(e) and attempt < MAX_PTL_RETRIES:
                truncated = truncate_head(source_messages)
                if truncated is None:
                    logger.warning("Auto compact: cannot truncate further, giving up")
                    return False
                source_messages = truncated
                prepared = _messages_to_prepared(source_messages)
                logger.info(
                    "Auto compact: prompt too long, truncated to %d messages (attempt %d/%d)",
                    len(source_messages), attempt + 1, MAX_PTL_RETRIES,
                )
                continue
            logger.warning("Auto compact: side_query failed: %s", e)
            return False

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

        logger.info("Auto compact: replaced %d messages with summary", len(prepared))
        return True

    return False
