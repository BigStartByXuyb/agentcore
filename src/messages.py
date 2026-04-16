"""Message construction helpers for the Claude API."""

from __future__ import annotations

from src.tools import ALL_TOOLS


def build_tool_schemas(
    allowed_tools: list[str] | None = None,
    tool_overrides: dict | None = None,
) -> list[dict]:
    """Extract tool schemas for the API tools parameter.

    If allowed_tools is provided, only include schemas for tools in the list.
    If tool_overrides is provided, use overridden ToolDef.schema for matching names.
    The Skill tool is always included so the LLM can invoke skills even when
    a skill restricts available tools.
    """
    overrides = tool_overrides or {}

    if allowed_tools is None:
        result = []
        for name, tool in ALL_TOOLS.items():
            if name in overrides:
                result.append(overrides[name].schema)
            else:
                result.append(tool.schema)
        return result

    # Always include Skill tool so LLM can chain skills
    always_include = {"Skill"}
    result = []
    for name, tool in ALL_TOOLS.items():
        if name in allowed_tools or name in always_include:
            if name in overrides:
                result.append(overrides[name].schema)
            else:
                result.append(tool.schema)
    return result


def build_tool_result_content(tool_use_id: str, content: str, is_error: bool = False) -> dict:
    """Build a single tool_result content block.

    When is_error is True, wraps the content in <tool_use_error> tags and
    sets the ``is_error`` flag so the LLM can structurally distinguish
    success from failure.  Mirrors Claude Code's tool_result construction
    in toolExecution.ts.
    """
    if is_error:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": f"<tool_use_error>{content}</tool_use_error>",
            "is_error": True,
        }
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
         "is_error": False
    }


# ---------------------------------------------------------------------------
# Thinking block cleanup — mirrors Claude Code's messages.ts
# ---------------------------------------------------------------------------

def _is_thinking_block(block: dict) -> bool:
    """Check if a content block is a thinking or redacted_thinking block."""
    return block.get("type") in ("thinking", "redacted_thinking")


def strip_thinking_blocks(messages: list[dict]) -> list[dict]:
    """Remove thinking/redacted_thinking blocks from all assistant messages.

    Thinking block signatures are bound to the API key + model that generated
    them.  After a credential change or model switch they become invalid and
    the API rejects them with a 400.  Stripping them lets the conversation
    continue.

    Mirrors Claude Code's stripSignatureBlocks() in messages.ts:5066.
    Returns a new list (does not mutate the input).
    """
    changed = False
    result: list[dict] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            result.append(msg)
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        filtered = [b for b in content if not _is_thinking_block(b)]
        if len(filtered) == len(content):
            result.append(msg)
        else:
            changed = True
            result.append({**msg, "content": filtered})

    return result if changed else messages


def filter_orphaned_thinking_messages(messages: list[dict]) -> list[dict]:
    """Remove assistant messages that contain ONLY thinking blocks.

    These orphaned thinking-only messages cause "thinking blocks cannot be
    modified" API errors.  They can appear when streaming yields separate
    messages per content block and interleaved user messages prevent proper
    merging.

    Mirrors Claude Code's filterOrphanedThinkingOnlyMessages() in
    messages.ts:4991.
    Returns a new list (does not mutate the input).
    """
    result: list[dict] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            result.append(msg)
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        if len(content) == 0:
            # Empty content — skip (can result from strip_thinking_blocks
            # removing all blocks from a thinking-only message)
            continue

        all_thinking = all(_is_thinking_block(b) for b in content)
        if not all_thinking:
            result.append(msg)
            # Orphaned thinking-only message — skip it

    return result
