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
from src.api import query_model
from src.compact.prompt import get_compact_prompt, get_compact_user_summary
from src.compact.grouping import truncate_head, MAX_PTL_RETRIES
from src.core.errors import AgentErrorCode, get_ptl_token_gap
from src.core.types import MessageHistory, Message
from src.providers.types import ProviderMessage

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


def _extract_summary(response: ProviderMessage) -> str | None:
    """Extract text summary from a successful ProviderMessage. None if empty."""
    if not response.content:
        return None
    raw_text = ""
    for block in response.content:
        if block.type == "text":
            raw_text += block.text
    return raw_text.strip() or None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def auto_compact(history: MessageHistory) -> bool:
    """Full compact with truncate-head retry on prompt_too_long.

    Manages truncation at the outer level so that thinking/non-thinking
    fallback shares the same truncation state (fixes state-loss bug where
    fallback would restart from untruncated history).

    For each truncation state:
      1. Try with thinking enabled (if configured)
      2. If PTL, try without thinking on the SAME truncated messages
      3. If still PTL, truncate and loop

    Returns True if compaction was performed, False otherwise.
    """
    thinking = config.THINKING_ENABLED
    max_tokens_thinking = (
        config.THINKING_BUDGET_TOKENS + config.AUTO_COMPACT_MAX_TOKENS
        if thinking else config.AUTO_COMPACT_MAX_TOKENS
    )
    max_tokens_no_thinking = config.AUTO_COMPACT_MAX_TOKENS

    source_messages = list(history.messages)
    if not source_messages:
        return False

    ptl_value = AgentErrorCode.API_PROMPT_TOO_LONG.value
    thinking_error_value = AgentErrorCode.API_THINKING_ERROR.value

    for attempt in range(MAX_PTL_RETRIES + 1):
        prepared = _messages_to_prepared(source_messages)
        if not prepared:
            return False

        compact_kwargs = dict(
            model=config.MODELS.compact,
            system=get_compact_prompt(),
            messages=prepared,
            tools=[],
        )

        response = await query_model(**compact_kwargs, max_tokens=max_tokens_thinking, thinking=thinking)

        if response.is_error and thinking and response.error_code in (ptl_value, thinking_error_value):
            logger.info(
                "Auto compact: thinking attempt got %s, trying without thinking",
                response.error_code,
            )
            response = await query_model(**compact_kwargs, max_tokens=max_tokens_no_thinking, thinking=False)

        if response.is_error:
            error_text = response.content[0].text if response.content else ""
            if response.error_code == ptl_value and attempt < MAX_PTL_RETRIES:
                token_gap = get_ptl_token_gap(error_text)
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
            logger.warning("Auto compact: failed with %s: %s", response.error_code, error_text)
            return False

        summary = _extract_summary(response)
        if not summary:
            logger.warning("Auto compact: LLM returned no text content")
            return False

        summary_text = get_compact_user_summary(summary, suppress_follow_up=True)
        history.replace_with_summary(summary_text)
        logger.info("Auto compact: replaced %d messages with summary", len(prepared))
        return True

    return False
