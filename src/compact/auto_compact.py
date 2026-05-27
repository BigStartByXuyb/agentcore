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
from dataclasses import dataclass

from src.core import config
from src.api import query_model
from src.compact.prompt import get_compact_prompt, get_compact_user_summary
from src.compact.grouping import truncate_head, MAX_PTL_RETRIES
from src.core.errors import is_prompt_too_long, get_ptl_token_gap
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


# ---------------------------------------------------------------------------
# Single-call result
# ---------------------------------------------------------------------------

@dataclass
class _CompactAttemptResult:
    """Result of a single compact API call — mirrors Claude Code's AssistantMessage pattern.

    Unified type for success and failure: caller checks ``is_error`` first,
    then ``error_type`` to decide recovery strategy.

    Success: is_error=False, summary has value
    Failure: is_error=True,  error_type classifies the failure,
             error_detail carries the raw error message
    """
    summary: str | None = None
    is_error: bool = False
    error_type: str | None = None      # "prompt_too_long" | "api_error" | "empty_response"
    error_detail: str | None = None    # raw error message (e.g. "150000 tokens > 128000")


async def _try_single_call(
    prepared_messages: list[Message],
    *,
    thinking: bool,
    max_tokens: int,
) -> _CompactAttemptResult:
    """Attempt one compact API call.

    Returns _CompactAttemptResult:
      - is_error=False, summary set: success
      - is_error=True, error_type="prompt_too_long": caller should truncate and retry
      - is_error=True, error_type="api_error"|"empty_response": caller should give up
    """
    try:
        response = await query_model(
            model=config.MODELS.compact,
            system=get_compact_prompt(),
            messages=prepared_messages,
            tools=[],
            max_tokens=max_tokens,
            thinking=thinking,
        )
    except Exception as e:
        if is_prompt_too_long(e):
            return _CompactAttemptResult(
                is_error=True,
                error_type="prompt_too_long",
                error_detail=str(e),
            )
        logger.warning("Auto compact: LLM call failed: %s", e)
        return _CompactAttemptResult(
            is_error=True,
            error_type="api_error",
            error_detail=str(e),
        )

    if not response or not response.content:
        logger.warning("Auto compact: LLM returned empty response")
        return _CompactAttemptResult(
            is_error=True,
            error_type="empty_response",
        )

    raw_text = ""
    for block in response.content:
        if block.type == "text":
            raw_text += block.text

    if not raw_text.strip():
        logger.warning("Auto compact: LLM returned no text content")
        return _CompactAttemptResult(
            is_error=True,
            error_type="empty_response",
        )

    return _CompactAttemptResult(summary=raw_text.strip())


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

    for attempt in range(MAX_PTL_RETRIES + 1):
        prepared = _messages_to_prepared(source_messages)
        if not prepared:
            return False

        # --- Try with thinking ---
        result = await _try_single_call(
            prepared, thinking=thinking, max_tokens=max_tokens_thinking,
        )

        # Any failure with thinking on → try without thinking on the SAME
        # truncation state before deciding next action.
        if result.is_error and thinking:
            logger.info(
                "Auto compact: thinking attempt got %s, trying without thinking",
                result.error_type,
            )
            result = await _try_single_call(
                prepared, thinking=False, max_tokens=max_tokens_no_thinking,
            )

        # Still PTL → truncate and retry
        if result.is_error and result.error_type == "prompt_too_long":
            if attempt < MAX_PTL_RETRIES:
                token_gap = get_ptl_token_gap(result.error_detail)
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
            logger.warning("Auto compact: PTL retries exhausted")
            return False

        # Other failure (api_error, empty_response, etc.)
        if result.is_error:
            logger.warning("Auto compact: failed with %s: %s", result.error_type, result.error_detail)
            return False

        # Success — is_error=False guarantees summary is set
        assert result.summary is not None
        summary_text = get_compact_user_summary(result.summary, suppress_follow_up=True)
        history.replace_with_summary(summary_text)
        logger.info("Auto compact: replaced %d messages with summary", len(prepared))
        return True

    return False
