"""Built-in slash commands (/clear, /resume, /sessions, /plan).

Each command is a standalone async function that receives and returns
a CommandContext — the mutable session state bundle. This keeps main.py
thin (just routing) and makes commands testable and reusable from
future frontends (web, TUI, etc.).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.core import config
from src.core.types import AgentState, MessageHistory, PlanPhase
from src.utils.file_state_cache import FileStateCache
from src.session import SessionStorage


@dataclass
class CommandContext:
    """Mutable session state passed to commands."""
    history: MessageHistory
    state: AgentState
    file_cache: FileStateCache
    storage: SessionStorage | None


class CommandResult:
    """Result of a command execution."""
    def __init__(self, ctx: CommandContext, handled: bool = True):
        self.ctx = ctx
        self.handled = handled


async def handle_clear(ctx: CommandContext) -> CommandContext:
    """Reset session state, start a new session."""
    from src.plan_mode import clear_slug_cache
    clear_slug_cache(ctx.state.agent_id)
    if hasattr(ctx.state, "_task_store") and ctx.state._task_store is not None:
        ctx.state._task_store.clear()

    if ctx.storage:
        ctx.storage.close()

    config.SESSION_ID = uuid.uuid4().hex[:12]
    ctx.history = MessageHistory()
    ctx.state = AgentState()
    ctx.file_cache = FileStateCache()

    if config.SESSION_PERSIST_ENABLED:
        ctx.storage = SessionStorage(config.SESSION_ID)
        ctx.storage.open()
    else:
        ctx.storage = None

    print("Context cleared.")
    return ctx


async def handle_resume(ctx: CommandContext, arg: str = "") -> CommandContext:
    """Restore a previous session. Shows picker if no ID specified."""
    if arg:
        target_id = arg
    else:
        sessions = SessionStorage.list_sessions()
        if not sessions:
            print("[No sessions found to resume]")
            return ctx
        print("Available sessions:")
        for i, s in enumerate(sessions, 1):
            ts = datetime.fromtimestamp(s["mtime"]).strftime("%m-%d %H:%M")
            marker = " (current)" if s["id"] == config.SESSION_ID else ""
            print(f"  [{i}] {s['id']}  {ts}  {s['preview']}{marker}")
        print("  [0] Cancel")
        try:
            choice = await asyncio.to_thread(input, "Select session: ")
            choice = choice.strip()
            if not choice or choice == "0":
                return ctx
            idx = int(choice) - 1
            if idx < 0 or idx >= len(sessions):
                print("[Invalid selection]")
                return ctx
            target_id = sessions[idx]["id"]
        except (ValueError, EOFError, KeyboardInterrupt):
            return ctx

    if target_id == config.SESSION_ID:
        print("[Already in this session]")
        return ctx

    try:
        ctx.history = SessionStorage.load(target_id)
        if ctx.storage:
            ctx.storage.close()
        config.SESSION_ID = target_id
        ctx.storage = SessionStorage(target_id)
        ctx.storage.open()
        ctx.state = AgentState()
        ctx.file_cache = FileStateCache()
        print(f"[Resumed session: {target_id}, {len(ctx.history)} messages]")
        _print_recent_messages(ctx.history)
    except FileNotFoundError:
        print(f"[Session '{target_id}' not found]")

    return ctx


async def handle_sessions(ctx: CommandContext) -> CommandContext:
    """List available sessions for the current project."""
    sessions = SessionStorage.list_sessions()
    if not sessions:
        print("[No sessions found]")
    else:
        for s in sessions:
            ts = datetime.fromtimestamp(s["mtime"]).strftime("%m-%d %H:%M")
            print(f"  {s['id']}  [{ts}]  {s['preview']}")
    return ctx


def handle_plan(ctx: CommandContext, arg: str = "") -> tuple[CommandContext, str | None]:
    """Toggle plan mode. Returns (ctx, user_input_to_process) or (ctx, None) if fully handled."""
    if ctx.state.plan_phase == PlanPhase.ACTIVE and not arg:
        ctx.state.plan_phase = PlanPhase.EXITING
        print("Plan mode deactivated.")
        return ctx, None

    if ctx.state.plan_phase != PlanPhase.ACTIVE:
        from src.plan_mode import enter_plan_mode
        ctx.state.plan_file_path = enter_plan_mode(session_id=ctx.state.agent_id)
        ctx.state.plan_phase = PlanPhase.ACTIVE
        print(f"Plan mode activated. Plan file: {ctx.state.plan_file_path}")

    if not arg:
        return ctx, None

    return ctx, arg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAX_PREVIEW_MESSAGES = 6
_MAX_CONTENT_LENGTH = 120


def _print_recent_messages(history: MessageHistory) -> None:
    """Print the last few user/assistant messages using on_event-like format."""
    # Only show actual conversation messages, skip meta/tool_result
    visible = [
        m for m in history.messages
        if m.msg_type in ("human", "assistant")
    ]
    if not visible:
        return

    show = visible[-_MAX_PREVIEW_MESSAGES:]
    if len(visible) > _MAX_PREVIEW_MESSAGES:
        print(f"  ... ({len(visible) - _MAX_PREVIEW_MESSAGES} earlier messages)")

    for msg in show:
        label = "human" if msg.msg_type == "human" else "main"
        content = _extract_preview_text(msg)
        if len(content) > _MAX_CONTENT_LENGTH:
            content = content[:_MAX_CONTENT_LENGTH] + "..."
        if content:
            print(f"  [{label}] {content}")


def _extract_preview_text(msg) -> str:
    """Extract displayable text from a Message."""
    if isinstance(msg.content, str):
        return msg.content.replace("\n", " ").strip()
    # list of ContentBlocks — collect all text blocks
    parts: list[str] = []
    for block in msg.content:
        if hasattr(block, "text"):
            parts.append(block.text.replace("\n", " ").strip())
    return " ".join(parts) if parts else ""
