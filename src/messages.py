"""Message construction helpers for the Claude API."""

from __future__ import annotations

from src.types import ToolResultContent, Attachment
from src.tools import ALL_TOOLS


# ---------------------------------------------------------------------------
# Metadata reminders (skill / agent listings)
# ---------------------------------------------------------------------------

def build_metadata_reminders(
    tools: list[str],
    *,
    use_sent_tracking: bool = True,
    skill_filter: list[str] | None = None,
    exclude_fork_skills: bool = False,
) -> list[Attachment]:
    """Build <system-reminder> user messages announcing available skills/agents.

    Returns a list of 0-2 Message objects ready to be injected into history.
    """
    tools_lower = {t.lower() for t in tools}
    reminders: list[Attachment] = []

    # --- Skill listing ---
    if "skill" in tools_lower:
        msg = _build_skill_reminder_msg(
            use_sent_tracking=use_sent_tracking,
            skill_filter=skill_filter,
            exclude_fork_skills=exclude_fork_skills,
        )
        if msg is not None:
            reminders.append(msg)

    # --- Agent listing ---
    if "agent" in tools_lower:
        msg = _build_agent_reminder_msg(use_sent_tracking=use_sent_tracking)
        if msg is not None:
            reminders.append(msg)

    return reminders


def _build_skill_reminder_msg(
    *,
    use_sent_tracking: bool,
    skill_filter: list[str] | None,
    exclude_fork_skills: bool,
) -> Attachment | None:
    """Build the skill reminder user message, honoring sub-agent isolation."""
    if use_sent_tracking:
        from src.skills import build_skill_reminder
        return build_skill_reminder()

    from src.skills import get_skills, format_skill_listing
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


def _build_agent_reminder_msg(*, use_sent_tracking: bool) -> Attachment | None:
    """Build the agent reminder user message, honoring sub-agent isolation."""
    if use_sent_tracking:
        from src.agents import build_agent_reminder
        return build_agent_reminder()

    from src.agents import get_agents, format_agent_listing
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

# tools checking and schema construction for the API "tools" parameter is centralized
def build_tool_schemas(
    allowed_tools: list[str] | None = None,
    tool_overrides: dict | None = None,
) -> list[dict]:
    """Extract tool schemas for the API tools parameter.

    If allowed_tools is provided, only include schemas for tools in the list.
    If tool_overrides is provided, use overridden ToolDef.schema for matching names.

    Previously the Skill tool was force-included via an always_include set.
    That's been removed: whether Skill is available is now fully controlled
    by allowed_tools / disallowed_tools in the ToolUseContext.  The Skill
    tool's context_modifier (inline mode) already includes "Skill" in its
    own allowed_tools list, so skills that restrict tools still work.
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

    result = []
    for name, tool in ALL_TOOLS.items():
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
