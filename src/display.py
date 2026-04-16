"""Default terminal display handler for agent events.

Provides:
  - default_handler()  — prints events to stdout (the current print logic)
  - consume_events()   — drains a run_agent_loop generator, dispatches events,
                         returns the final text

This module is the ONLY place in the codebase that prints agent output.
The agent loop itself yields events; this module decides how to render them.
"""

from __future__ import annotations

from typing import Callable, Generator

from src.events import (
    AgentEvent,
    TextDelta,
    TextBlock,
    ThinkingBlock,
    ToolStart,
    ToolEnd,
    ErrorEvent,
    Recovery,
    TokenUsage,
    RetryNotice,
    SubAgentStart,
    SubAgentEnd,
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

    elif isinstance(event, ThinkingBlock):
        text = event.thinking
        if text:
            display = text if len(text) <= _THINKING_DISPLAY_MAX else text[:_THINKING_DISPLAY_MAX] + "..."
            print(f"  [{label}:thinking] {display}")

    elif isinstance(event, ToolStart):
        summary = _summarize_input(event.tool_input)
        print(f"  [{label}:tool] {event.tool_name}({summary})")

    elif isinstance(event, ToolEnd):
        status = "error" if event.is_error else "ok"
        summary = event.result_summary[:80] if event.result_summary else ""
        print(f"  [{label}:tool-end] {event.tool_name} [{status}] {summary}")

    elif isinstance(event, ErrorEvent):
        print(f"  [{label}:error] {event.error_text[:120]}")

    elif isinstance(event, Recovery):
        print(f"  [{label}:recovery] {event.message}")

    elif isinstance(event, TokenUsage):
        parts = [f"in={event.input_tokens}", f"out={event.output_tokens}"]
        if event.thinking_tokens > 0:
            parts.append(f"thinking={event.thinking_tokens}")
        print(f"\n[{label} tokens: {' '.join(parts)}]")

    elif isinstance(event, RetryNotice):
        print(f"  [retry] API error, retrying in {event.delay:.1f}s ({event.attempt}/{event.max_attempts})...")

    elif isinstance(event, SubAgentStart):
        print(f"  [{label}] Starting (depth={event.depth})...")

    elif isinstance(event, SubAgentEnd):
        print(f"  [{label}] Completed.")


# ---------------------------------------------------------------------------
# Event consumer
# ---------------------------------------------------------------------------

def consume_events(
    gen: Generator[AgentEvent, None, str],
    handler: Callable[[AgentEvent], None] | None = None,
) -> str:
    """Drain a run_agent_loop generator, dispatch events, return final text.

    If handler is None, events are silently discarded (useful for sub-agents
    that should not produce terminal output).
    """
    try:
        while True:
            event = next(gen)
            if handler is not None:
                handler(event)
    except StopIteration as e:
        return e.value


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
