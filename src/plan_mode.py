"""Plan Mode — state management and helpers.

Corresponds to Claude Code's plan mode: restricts tools to read-only + plan
file writing, injects plan mode attachment, and provides post-compact recovery.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from src.types import Attachment, ToolDef, ToolResult


# Module-level slug cache: session_id → slug string.
# Same session reuses the same plan file. /clear calls clear_slug_cache().
_slug_cache: dict[str, str] = {}


def clear_slug_cache(session_id: str) -> None:
    """Remove cached slug for a session (called on /clear)."""
    _slug_cache.pop(session_id, None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enter_plan_mode(session_id: str = "main") -> str:
    """Return a plan file path for this session.

    The slug is cached per session_id so the same session reuses the
    same plan file. Call clear_slug_cache() on /clear to reset.
    """
    plan_dir = os.path.join(str(Path.home()), ".my-agent", "plans")
    os.makedirs(plan_dir, exist_ok=True)

    if session_id in _slug_cache:
        slug = _slug_cache[session_id]
    else:
        slug = uuid.uuid4().hex[:12]
        _slug_cache[session_id] = slug

    return os.path.join(plan_dir, f"plan-{slug}.md")


def build_plan_mode_attachment(plan_file_path: str) -> Attachment:
    """Build the plan mode constraint attachment injected into user messages."""
    content = (
        "<system-reminder>\n"
        "Plan mode is active. You MUST plan before executing.\n\n"
        "## Constraints\n"
        "- You may ONLY use read-only tools (read_file, grep, bash) to explore the codebase\n"
        "- You may ONLY write to the plan file specified below\n"
        "- Do NOT modify any other files\n"
        "- Do NOT execute code changes\n"
        "- When your plan is complete, call the ExitPlanMode tool\n\n"
        f"## Plan File\n"
        f"Write your structured plan to: {plan_file_path}\n"
        "Use the write_file tool to create or update this file.\n\n"
        "## Plan Format\n"
        "Your plan should include:\n"
        "1. Summary of the task and goal\n"
        "2. Files to create or modify (with paths)\n"
        "3. Step-by-step implementation approach\n"
        "4. Dependencies and sequencing\n"
        "5. Potential risks or edge cases\n"
        "</system-reminder>"
    )
    return Attachment(type="plan_mode", content=content)


def build_plan_content_attachment(plan_file_path: str) -> Attachment | None:
    """Read the plan file from disk and wrap as an attachment (for post-compact recovery)."""
    if not os.path.exists(plan_file_path):
        return None
    try:
        with open(plan_file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    if not content.strip():
        return None
    return Attachment(
        type="system_reminder",
        content=(
            f"<system-reminder>\n"
            f"Current plan file content ({plan_file_path}):\n\n"
            f"{content}\n"
            f"</system-reminder>"
        ),
    )


def build_plan_exit_attachment(plan_file_path: str) -> Attachment:
    """One-time attachment injected the turn after exiting plan mode."""
    plan_exists = os.path.exists(plan_file_path)
    plan_ref = (
        f" The plan file is located at {plan_file_path} if you need to reference it."
        if plan_exists
        else ""
    )
    content = (
        "<system-reminder>\n"
        "## Exited Plan Mode\n\n"
        "You have exited plan mode. You can now make edits, run tools, "
        f"and take actions.{plan_ref}\n"
        "</system-reminder>"
    )
    return Attachment(type="system_reminder", content=content)


def make_plan_mode_overrides(plan_file_path: str, registry) -> dict[str, ToolDef]:
    """Create tool_overrides that restrict write_file/edit_file to the plan file only."""
    abs_plan = os.path.normpath(os.path.abspath(plan_file_path))
    overrides: dict[str, ToolDef] = {}

    for tool_name in ("write_file", "edit_file"):
        original = registry.get(tool_name)
        if original is None:
            continue
        overrides[tool_name] = _wrap_with_path_guard(original, abs_plan, plan_file_path)

    return overrides


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _wrap_with_path_guard(original: ToolDef, abs_plan: str, display_path: str) -> ToolDef:
    """Wrap a file-writing tool to only allow writes to the plan file."""

    async def _guarded_executor(inputs: dict, ctx) -> ToolResult:
        target = os.path.normpath(os.path.abspath(inputs.get("file_path", "")))
        if target != abs_plan:
            return ToolResult(data={
                "type": "error",
                "content": (
                    f"Plan mode: writes are restricted to the plan file "
                    f"({display_path}). Cannot write to {inputs.get('file_path')}."
                ),
            })
        return await original.executor(inputs, ctx)

    return ToolDef(
        schema=original.schema,
        executor=_guarded_executor,
        map_result=original.map_result,
        display_result=original.display_result,
        is_read_only=original.is_read_only,
    )
