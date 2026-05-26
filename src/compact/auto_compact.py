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

from src.core import config
from src.api import side_query
from src.compact.prompt import get_compact_prompt, get_compact_user_summary
from src.compact.grouping import truncate_head, MAX_PTL_RETRIES
from src.core.errors import is_prompt_too_long
from src.core.types import MessageHistory, Message

logger = logging.getLogger(__name__)

AUTOCOMPACT_BUFFER_TOKENS = 13_000
MAX_CONSECUTIVE_COMPACT_FAILURES = 3
BLOCKING_LIMIT_BUFFER_TOKENS = 3_000


def should_auto_compact(estimated_tokens: int) -> bool:
    """Check if estimated token count is near context window limit."""
    threshold = config.MAX_CONTEXT_WINDOW - AUTOCOMPACT_BUFFER_TOKENS
    return estimated_tokens >= threshold


def is_at_blocking_limit(estimated_tokens: int) -> bool:
    """Check if tokens are at the hard limit — too dangerous to call API."""
    return estimated_tokens >= config.MAX_CONTEXT_WINDOW - BLOCKING_LIMIT_BUFFER_TOKENS


def _messages_to_prepared(messages: list[Message]) -> list[Message]:
    """Expand attachments + merge same-role, reusing MessageHistory's logic."""
    return MessageHistory(messages=messages).prepare_messages()


async def auto_compact(history: MessageHistory) -> bool:
    """Full compact with truncate-head retry on prompt_too_long.

    Uses the main model with thinking enabled (matching Claude Code's
    forked-agent compaction path). Falls back to main model without
    thinking on failure.

    If the compaction side_query itself gets prompt_too_long, drops the
    oldest 20% of message groups and retries (up to MAX_PTL_RETRIES times).

    Returns True if compaction was performed, False otherwise.
    """
    thinking = config.THINKING_ENABLED
    # When thinking is on, max_tokens must cover both thinking budget and output
    max_tokens = (
        config.THINKING_BUDGET_TOKENS + config.AUTO_COMPACT_MAX_TOKENS
        if thinking else config.AUTO_COMPACT_MAX_TOKENS
    )

    # Primary path: main model + thinking
    result = await _try_compact(history, thinking=thinking, max_tokens=max_tokens)
    if result is not None:
        return result

    # Fallback: main model, no thinking (matches Claude Code's streaming fallback)
    if thinking:
        logger.info("Auto compact: retrying without thinking")
        result = await _try_compact(
            history, thinking=False, max_tokens=config.AUTO_COMPACT_MAX_TOKENS,
        )
        if result is not None:
            return result

    return False


async def _try_compact(
    history: MessageHistory,
    *,
    thinking: bool,
    max_tokens: int,
) -> bool | None:
    """Single compact attempt with PTL retry loop.

    Returns True on success, False on unrecoverable failure,
    None if the caller should try a different strategy (fallback).
    """
    prepared = history.prepare_messages()
    if not prepared:
        return False

    source_messages = list(history.messages)

    for attempt in range(MAX_PTL_RETRIES + 1):
        try:
            response = await side_query(
                model=config.MODEL,
                system=get_compact_prompt(),
                messages=prepared,
                max_tokens=max_tokens,
                thinking=thinking,
            )
        except Exception as e:
            if is_prompt_too_long(e) and attempt < MAX_PTL_RETRIES:
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
            return None  # signal caller to try fallback

        if not response or not response.content:
            logger.warning("Auto compact: LLM returned empty response")
            return None

        raw_text = ""
        for block in response.content:
            if block.type == "text":
                raw_text += block.text

        if not raw_text.strip():
            logger.warning("Auto compact: LLM returned no text content")
            return None

        summary_text = get_compact_user_summary(raw_text, suppress_follow_up=True)
        history.replace_with_summary(summary_text)

        logger.info("Auto compact: replaced %d messages with summary", len(prepared))
        return True

    return None
