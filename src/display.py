"""Default terminal display handler for agent events.

Provides:
  - default_handler()         — prints events to stdout
  - make_interactive_handler() — wraps a base handler with permission-prompt logic
"""

from __future__ import annotations

import asyncio
from typing import Callable

from src.types import EventCallback
from src.events import (
    AgentEvent,
    TextDelta,
    TextBlock,
    ThinkingBlock,
    ThinkingDelta,
    ToolStart,
    ToolEnd,
    ErrorEvent,
    Recovery,
    TokenUsage,
    RetryNotice,
    SubAgentStart,
    SubAgentEnd,
    PermissionRequest,
    PermissionDenied,
)

_THINKING_DISPLAY_MAX = 200


def default_handler(event: AgentEvent) -> None:
    """Print an agent event to the terminal.

    This replicates the exact output format that was previously scattered
    across agent_loop.py, api.py, and tools/*.py as inline print() calls.
    """
    label = event.label

    if isinstance(event, TextDelta):
        if event.first:
            print(f"  [{label}] ", end="", flush=True)
        print(event.delta, end="", flush=True)

    elif isinstance(event, TextBlock):
        print(f"  [{label}:text] {event.text}")

    elif isinstance(event, ThinkingDelta):
        if event.first:
            print(f"\n  [{label}:thinking] ", end="", flush=True)
        print(event.delta, end="", flush=True)

    elif isinstance(event, ThinkingBlock):
        text = event.thinking
        if text:
            display = text if len(text) <= _THINKING_DISPLAY_MAX else text[:_THINKING_DISPLAY_MAX] + "..."
            print(f"\n  [{label}:thinking] {display}")

    elif isinstance(event, ToolStart):
        summary = _summarize_input(event.tool_input)
        print(f"\n  [{label}:tool] {event.tool_name}({summary})")

    elif isinstance(event, ToolEnd):
        status = "error" if event.is_error else "ok"
        summary = event.result_summary[:80] if event.result_summary else ""
        print(f"\n  [{label}:tool-end] {event.tool_name} [{status}] {summary}")

    elif isinstance(event, ErrorEvent):
        print(f"\n  [{label}:error] {event.error_text[:120]}")

    elif isinstance(event, Recovery):
        print(f"\n  [{label}:recovery] {event.message}")

    elif isinstance(event, TokenUsage):
        parts = [f"in={event.input_tokens}", f"out={event.output_tokens}"]
        if event.thinking_tokens > 0:
            parts.append(f"thinking={event.thinking_tokens}")
        print(f"\n[{label} tokens: {' '.join(parts)}]")

    elif isinstance(event, RetryNotice):
        print(f"\n  [retry] API error, retrying in {event.delay:.1f}s ({event.attempt}/{event.max_attempts})...")

    elif isinstance(event, SubAgentStart):
        print(f"\n  [{label}] Starting (depth={event.depth})...")

    elif isinstance(event, SubAgentEnd):
        print(f"\n  [{label}] Completed.")

    elif isinstance(event, PermissionDenied):
        print(f"\n  [{label}:denied] {event.tool_name}: {event.message}")


# ---------------------------------------------------------------------------
# Interactive handler factory
# ---------------------------------------------------------------------------

def make_interactive_handler(
    base_handler: Callable[[AgentEvent], None],
    *,
    interactive: bool = True,
) -> EventCallback:
    """Wrap *base_handler* with permission-prompt logic.

    For PermissionRequest events (interactive=True): schedule a user prompt
    via asyncio.to_thread and resolve the Future so the tool runner resumes.
    For all other events: delegate to *base_handler*.
    """

    def handler(event: AgentEvent) -> None:
        if isinstance(event, PermissionRequest) and event.future is not None:
            if interactive:
                summary = _summarize_input(event.tool_input)
                prompt = f"\n  Allow {event.tool_name}({summary})? [y/n/always]: "

                async def _resolve() -> None:
                    answer = await asyncio.to_thread(input, prompt)
                    event.future.set_result(answer.strip().lower())

                asyncio.create_task(_resolve())
            else:
                event.future.set_result("n")
        else:
            base_handler(event)

    return handler


# ---------------------------------------------------------------------------
# Helpers (moved from agent_loop.py)
# ---------------------------------------------------------------------------

def _summarize_input(tool_input: dict) -> str:
    """Short summary of tool input for terminal display."""
    if "command" in tool_input:
        cmd = tool_input["command"]
        return cmd if len(cmd) <= 80 else cmd[:77] + "..."
    if "file_path" in tool_input:
        return tool_input["file_path"]
    if "pattern" in tool_input:
        return tool_input["pattern"]
    return str(tool_input)[:80]
