"""Message grouping and truncation utilities for auto-compact retry.

Provides:
  - group_messages_by_round()       — group messages by API round-trip boundaries
  - truncate_head()                 — drop oldest groups for prompt-too-long retry
  - ensure_tool_result_pairing()    — defensive repair for orphaned tool_use/tool_result

Corresponds to Claude Code's:
  - src/services/compact/grouping.ts  → groupMessagesByApiRound
  - src/services/compact/compact.ts   → truncateHeadForPTLRetry
  - src/utils/messages.ts             → ensureToolResultPairing
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.core.types import (
    Message,
    TextContent,
    ToolUseContent,
    ToolResultContent,
)

if TYPE_CHECKING:
    from src.core.types import ContentBlock

MAX_PTL_RETRIES = 3
PTL_RETRY_MARKER = '[earlier conversation truncated for compaction retry]'


# ---------------------------------------------------------------------------
# Group messages by API round-trip
# ---------------------------------------------------------------------------

def group_messages_by_round(messages: list[Message]) -> list[list[Message]]:
    """Group messages by API round-trip boundaries.

    Each group starts with an assistant message (role="assistant",
    msg_type="assistant") and includes all subsequent messages until
    the next assistant message.

    This is more granular than per-user-turn grouping: a single user
    prompt can trigger many API round-trips in agentic sessions
    (tool_use → tool_result → next assistant → ...).
    """
    if not messages:
        return []

    groups: list[list[Message]] = []
    current: list[Message] = []

    for msg in messages:
        if (
            msg.role == "assistant"
            and msg.msg_type == "assistant"
            and current
        ):
            groups.append(current)
            current = [msg]
        else:
            current.append(msg)

    if current:
        groups.append(current)

    return groups


# ---------------------------------------------------------------------------
# Truncate oldest groups for prompt-too-long retry
# ---------------------------------------------------------------------------

def truncate_head(
    messages: list[Message],
    drop_ratio: float = 0.2,
    token_gap: int | None = None,
) -> list[Message] | None:
    """Drop the oldest N groups of messages, return the remainder.

    - Groups by API round-trip (group_messages_by_round)
    - When *token_gap* is given, drops groups until estimated tokens >= gap
    - Otherwise drops max(1, total_groups * drop_ratio) oldest groups
    - At least 1 group must remain (returns None if impossible)
    - If the truncated result starts with an assistant message,
      prepends a synthetic user message (PTL_RETRY_MARKER) to satisfy
      API constraints
    - Runs ensure_tool_result_pairing as a defensive final pass

    Returns None if truncation is impossible (0 or 1 groups).
    """
    # Strip our own marker from a previous retry before grouping.
    # Otherwise it becomes its own group and the drop ratio stalls.
    if (
        messages
        and messages[0].role == "user"
        and messages[0].msg_type == "meta"
        and isinstance(messages[0].content, str)
        and messages[0].content == PTL_RETRY_MARKER
    ):
        messages = messages[1:]

    groups = group_messages_by_round(messages)

    if len(groups) <= 1:
        return None

    if token_gap is not None:
        from src.utils.tokens import rough_estimate_messages
        n_drop = 0
        accumulated = 0
        for g in groups[:-1]:  # keep at least 1 group
            accumulated += rough_estimate_messages(g)
            n_drop += 1
            if accumulated >= token_gap:
                break
        n_drop = max(1, n_drop)
    else:
        n_drop = max(1, int(len(groups) * drop_ratio))

    remaining_groups = groups[n_drop:]

    if not remaining_groups:
        return None

    result: list[Message] = []
    for g in remaining_groups:
        result.extend(g)

    if result and result[0].role == "assistant":
        result.insert(0, Message(
            role="user",
            content=PTL_RETRY_MARKER,
            msg_type="meta",
        ))

    result = ensure_tool_result_pairing(result)
    return result


# ---------------------------------------------------------------------------
# Defensive tool_use / tool_result pairing repair
# ---------------------------------------------------------------------------

def ensure_tool_result_pairing(messages: list[Message]) -> list[Message]:
    """Ensure every tool_use has a matching tool_result and vice versa.

    Forward pass: if an assistant message contains tool_use(id=X) but no
    subsequent user message has tool_result(tool_use_id=X), append a
    synthetic error tool_result.

    Reverse pass: if a user message contains tool_result(tool_use_id=Y)
    but no preceding assistant message has tool_use(id=Y), strip that
    block. If the message becomes empty, replace content with placeholder.
    """
    tool_use_ids: set[str] = set()
    tool_result_ids: set[str] = set()

    for msg in messages:
        if not isinstance(msg.content, list):
            continue
        for block in msg.content:
            if isinstance(block, ToolUseContent):
                tool_use_ids.add(block.id)
            elif isinstance(block, ToolResultContent):
                tool_result_ids.add(block.tool_use_id)

    orphaned_uses = tool_use_ids - tool_result_ids
    orphaned_results = tool_result_ids - tool_use_ids

    if not orphaned_uses and not orphaned_results:
        return messages

    result: list[Message] = []

    for msg in messages:
        if not isinstance(msg.content, list):
            result.append(msg)
            continue

        if msg.role == "user":
            filtered: list[ContentBlock] = [
                b for b in msg.content
                if not (isinstance(b, ToolResultContent) and b.tool_use_id in orphaned_results)
            ]
            if not filtered:
                filtered = [TextContent(text="[truncated]")]  # type: ignore[list-item]
            new_msg = Message(
                role=msg.role,
                content=filtered,
                msg_type=msg.msg_type,
                attachments=msg.attachments,
                timestamp=msg.timestamp,
            )
            result.append(new_msg)
        else:
            result.append(msg)

    if orphaned_uses:
        synthetic_blocks: list[ContentBlock] = [
            ToolResultContent(  # type: ignore[list-item]
                tool_use_id=uid,
                content="[Error: tool result lost during context truncation]",
                is_error=True,
            )
            for uid in orphaned_uses
        ]
        if result and result[-1].role == "user" and isinstance(result[-1].content, list):
            result[-1].content.extend(synthetic_blocks)
        else:
            result.append(Message(
                role="user",
                content=synthetic_blocks,
                msg_type="tool_result",
            ))

    return result
