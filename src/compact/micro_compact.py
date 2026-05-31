"""Layer 1: Micro Compact — lightweight tool_result clearing before API calls.

No LLM call. Replaces old tool_result content with a short placeholder.
Groups tool calls by "round" (one assistant response = one round).
Keeps the most recent N rounds intact, clears the rest.

When prompt cache is enabled, only clears when cache has expired
(modifying old messages while cache is warm would invalidate it).

Corresponds to Claude Code's src/services/compact/microCompact.ts.
"""

from __future__ import annotations

import logging
import time

from src.core import config
from src.core.types import Message, ToolResultContent, ToolUseContent

logger = logging.getLogger(__name__)

CLEARED_MESSAGE = "[Old tool result content cleared]"


def should_micro_compact(messages: list[Message]) -> bool:
    """Decide whether micro compact should run right now."""
    if not config.get().micro_compact_enabled:
        return False

    if not config.get().prompt_cache_enabled:
        return True

    last_assistant = _find_last_assistant(messages)
    if last_assistant is None:
        return False

    gap_seconds = time.time() - last_assistant.timestamp
    return gap_seconds > config.get().prompt_cache_ttl_minutes * 60


def micro_compact(messages: list[Message], *, keep_recent: int | None = None) -> int:
    """Clear old tool_result content in-place, grouped by round.

    A "round" = all tool_use blocks in a single assistant message.
    Keeps the most recent `keep_recent` rounds intact, clears older ones.

    Returns the number of tool results cleared.
    """
    if keep_recent is None:
        keep_recent = config.get().micro_compact_keep_recent
    keep_recent = max(1, keep_recent)

    rounds = _collect_rounds(messages)
    if len(rounds) <= keep_recent:
        return 0

    clear_ids: set[str] = set()
    for round_ids in rounds[:-keep_recent]:
        clear_ids.update(round_ids)

    if not clear_ids:
        return 0

    cleared = 0
    for msg in messages:
        if msg.role != "user" or msg.msg_type != "tool_result":
            continue
        if not isinstance(msg.content, list):
            continue
        for i, block in enumerate(msg.content):
            if (
                isinstance(block, ToolResultContent)
                and block.tool_use_id in clear_ids
                and block.content != CLEARED_MESSAGE
            ):
                msg.content[i] = ToolResultContent(
                    tool_use_id=block.tool_use_id,
                    content=CLEARED_MESSAGE,
                    is_error=block.is_error,
                )
                cleared += 1

    if cleared:
        logger.debug(
            "Micro compact: cleared %d tool results from %d rounds, kept last %d rounds",
            cleared, len(rounds) - keep_recent, keep_recent,
        )

    return cleared


def _collect_rounds(messages: list[Message]) -> list[list[str]]:
    """Group tool_use IDs by round (one assistant message = one round).

    Returns a list of rounds, each round is a list of tool_use IDs.
    Only includes rounds that have at least one tool_use.
    """
    rounds: list[list[str]] = []
    for msg in messages:
        if msg.role != "assistant":
            continue
        if not isinstance(msg.content, list):
            continue
        round_ids: list[str] = []
        for block in msg.content:
            if isinstance(block, ToolUseContent):
                round_ids.append(block.id)
        if round_ids:
            rounds.append(round_ids)
    return rounds


def _find_last_assistant(messages: list[Message]) -> Message | None:
    """Find the most recent assistant message."""
    for msg in reversed(messages):
        if msg.role == "assistant":
            return msg
    return None
