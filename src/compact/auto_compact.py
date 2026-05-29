"""Layer 2: Auto Compact — LLM-based conversation summarization (Full Compact).

When token usage approaches the context window limit, calls a side LLM
to summarize the entire conversation, then replaces history with the summary.

Trigger order per API call:
  1. micro_compact (Layer 1, lossless)
  2. estimate tokens
  3. auto_compact if over threshold (Layer 2, lossy)

Includes truncate-head retry for when the compaction LLM call itself
exceeds the context window (prompt_too_long on the compact LLM call).

Corresponds to Claude Code's src/services/compact/compactHistory.ts.
"""

from __future__ import annotations

import logging

from src.core import config
from src.compact.prompt import get_compact_prompt, get_compact_user_summary
from src.compact.grouping import truncate_head, MAX_PTL_RETRIES
from src.core.errors import get_ptl_token_gap
from src.core.types import MessageHistory, Message, ToolUseContext

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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def auto_compact(parent_context: ToolUseContext) -> bool:
    """Full compact with truncate-head retry on prompt_too_long.

    Uses run_agent_loop as the unified LLM entry point (query_source="compact"
    disables internal compaction to prevent recursion). Thinking is disabled
    for compact (no need for deep reasoning, saves output tokens).

    Reuses the parent's system_prompt for prompt cache prefix sharing.
    The compact instruction is appended as a user message.

    Returns True if compaction was performed, False otherwise.
    """
    history = parent_context.messages
    source_messages = list(history.messages)
    if not source_messages:
        return False

    compact_prompt = get_compact_prompt()

    for attempt in range(MAX_PTL_RETRIES + 1):
        prepared = _messages_to_prepared(source_messages)
        if not prepared:
            return False

        from src.agent_loop import run_agent_loop

        compact_history = MessageHistory(prepared)
        compact_history.add_user(compact_prompt)
        compact_context = ToolUseContext(
            messages=compact_history,
            tools=[],
            system_prompt=parent_context.system_prompt,
            label="compact",
            thinking=False,
        )

        result = await run_agent_loop(
            tool_use_context=compact_context,
            max_turns=1,
            query_source="compact",
            on_event=lambda _: None,
        )

        if result.ok and result.text:
            summary_text = get_compact_user_summary(result.text, suppress_follow_up=True)
            history.replace_with_summary(summary_text)
            logger.info("Auto compact: replaced %d messages with summary", len(prepared))
            return True

        if result.reason == "prompt_too_long" and attempt < MAX_PTL_RETRIES:
            token_gap = get_ptl_token_gap(result.text)
            truncated = truncate_head(source_messages, token_gap=token_gap)
            if truncated is None:
                logger.warning("Auto compact: cannot truncate further, giving up")
                return False
            source_messages = truncated
            logger.info(
                "Auto compact: prompt too long, truncated to %d messages (attempt %d/%d)",
                len(source_messages), attempt + 1, MAX_PTL_RETRIES,
            )
            continue

        logger.warning("Auto compact: failed with reason=%s: %s", result.reason, result.text[:200] if result.text else "")
        return False

    return False
