"""Message construction helpers for the Claude API."""

from __future__ import annotations

from src.types import ToolResultContent, Attachment


# ---------------------------------------------------------------------------
# Metadata reminders (skill / agent listings)
# ---------------------------------------------------------------------------

def build_skill_reminder(
    tools: list[str],
    *,
    use_sent_tracking: bool = True,
    force: bool = False,
    skill_filter: list[str] | None = None,
    exclude_fork_skills: bool = False,
) -> Attachment | None:
    """Build skill listing attachment — the single public API for skill injection.

    Parameters:
        tools:              Current available tool names; returns None if 'skill' not present.
        use_sent_tracking:  If True, only send new/unsent skills (main agent multi-turn).
                            If False, send all skills every time (sub-agent one-shot).
        force:              If True with sent_tracking, force full re-send (e.g. after compact).
        skill_filter:       Only include skills with these names (sub-agent precise mode).
        exclude_fork_skills: Skip fork-mode skills (sub-agent can't nest agent loops).
    """
    if "skill" not in {t.lower() for t in tools}:
        return None

    from src.skills import build_skill_attachment, get_skills, format_skill_listing

    if use_sent_tracking:
        return build_skill_attachment(
            force=force,
            exclude_fork=exclude_fork_skills,
            filter_names=skill_filter,
        )

    all_skills = get_skills()
    if not all_skills:
        return None
    listing = format_skill_listing(
        all_skills,
        exclude_fork=exclude_fork_skills,
        filter_names=skill_filter,
    )
    if not listing:
        return None
    return Attachment(
        type="system_reminder",
        content=f"<system-reminder>\n{listing}\n</system-reminder>",
    )


def build_agent_reminder(
    tools: list[str],
    *,
    use_sent_tracking: bool = True,
    force: bool = False,
) -> Attachment | None:
    """Build agent listing attachment — the single public API for agent injection.

    Parameters:
        tools:              Current available tool names; returns None if 'agent' not present.
        use_sent_tracking:  If True, only send new/unsent agents (main agent multi-turn).
                            If False, send all agents every time (sub-agent one-shot).
        force:              If True with sent_tracking, force full re-send (e.g. after compact).
    """
    if "agent" not in {t.lower() for t in tools}:
        return None

    from src.agents import build_agent_attachment, get_agents, format_agent_listing

    if use_sent_tracking:
        return build_agent_attachment(force=force)

    all_agents = get_agents()
    if not all_agents:
        return None
    listing = format_agent_listing(all_agents)
    if not listing:
        return None
    return Attachment(
        type="system_reminder",
        content=f"<system-reminder>\n{listing}\n</system-reminder>",
    )


def build_memory_index_reminder() -> Attachment | None:
    """Build memory index (MEMORY.md) attachment — synchronous, always injected."""
    from src.memory.prompt import build_memory_user_message
    content = build_memory_user_message()
    if not content:
        return None
    return Attachment(
        type="memory_index",
        content=f"<memory-index>\n{content}\n</memory-index>",
    )


# tools checking and schema construction for the API "tools" parameter is centralized
def build_tool_schemas(
    registry,
    allowed_tools: list[str] | None = None,
    tool_overrides: dict | None = None,
) -> list[dict]:
    """Extract tool schemas for the API tools parameter.

    If allowed_tools is provided, only include schemas for tools in the list.
    If tool_overrides is provided, use overridden ToolDef.schema for matching names.
    """
    overrides = tool_overrides or {}

    if allowed_tools is None:
        result = []
        for name, tool in registry.items():
            if name in overrides:
                result.append(overrides[name].schema)
            else:
                result.append(tool.schema)
        return result

    result = []
    for name, tool in registry.items():
        if name in allowed_tools:
            if name in overrides:
                result.append(overrides[name].schema)
            else:
                result.append(tool.schema)
    return result


def build_tool_result_content(tool_use_id: str, content: str, is_error: bool = False) -> ToolResultContent:
    """Build a single tool_result content block."""
    if is_error:
        return ToolResultContent(
            tool_use_id=tool_use_id,
            content=f"<tool_use_error>{content}</tool_use_error>",
            is_error=True,
        )
    return ToolResultContent(
        tool_use_id=tool_use_id,
        content=content,
    )


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
