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
    CompactCircuitBreaker,
    BlockingLimitReached,
    UserQuestionRequest,
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
        if event.display_text:
            print(f"\n  [{label}:tool-end] {event.tool_name} [{status}]")
            print(event.display_text)
        else:
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

    elif isinstance(event, CompactCircuitBreaker):
        print(f"\n  [{label}:warning] {event.message}")

    elif isinstance(event, BlockingLimitReached):
        print(f"\n  [{label}:error] {event.message}")


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


def _ask_single_question(q: dict) -> dict[str, str]:
    """Present one question with numbered options in the terminal.

    Returns {question_text: selected_answer}.
    """
    question_text = q.get("question", "")
    header = q.get("header", "")
    options: list[dict] = q.get("options", [])
    multi = q.get("multiSelect", False)

    print(f"\n  [{header}] {question_text}")
    for i, opt in enumerate(options, 1):
        label = opt.get("label", "")
        desc = opt.get("description", "")
        print(f"    {i}. {label} — {desc}")
    other_idx = len(options) + 1
    print(f"    {other_idx}. Other — provide custom input")

    if multi:
        raw = input(f"  Select options (comma-separated, e.g. 1,3) [1-{other_idx}]: ").strip()
        selected_labels: list[str] = []
        for part in raw.split(","):
            part = part.strip()
            if not part.isdigit():
                continue
            idx = int(part)
            if 1 <= idx <= len(options):
                selected_labels.append(options[idx - 1].get("label", ""))
            elif idx == other_idx:
                custom = input("  Your input: ").strip()
                if custom:
                    selected_labels.append(custom)
        answer = ", ".join(selected_labels) if selected_labels else options[0].get("label", "")
    else:
        raw = input(f"  Select [1-{other_idx}]: ").strip()
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(options):
                answer = options[idx - 1].get("label", "")
            elif idx == other_idx:
                answer = input("  Your input: ").strip()
            else:
                answer = options[0].get("label", "")
        else:
            answer = options[0].get("label", "")

    return {question_text: answer}


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
                if event.custom_prompt:
                    async def _resolve_custom() -> None:
                        answer = await asyncio.to_thread(input, event.custom_prompt)
                        answer = answer.strip().lower()
                        if answer in ("y", "yes"):
                            event.future.set_result("y")
                        else:
                            feedback = await asyncio.to_thread(
                                input, "  Feedback (press Enter to skip): "
                            )
                            feedback = feedback.strip()
                            if feedback:
                                event.future.set_result(f"n:{feedback}")
                            else:
                                event.future.set_result("n")
                    asyncio.create_task(_resolve_custom())
                else:
                    summary = _summarize_input(event.tool_input)
                    prompt = f"\n  Allow {event.tool_name}({summary})? [y/n/always]: "

                    async def _resolve() -> None:
                        answer = await asyncio.to_thread(input, prompt)
                        event.future.set_result(answer.strip().lower())

                    asyncio.create_task(_resolve())
            else:
                event.future.set_result("n")

        elif isinstance(event, UserQuestionRequest) and event.future is not None:
            if interactive:
                async def _resolve_questions() -> None:
                    answers: dict[str, str] = {}
                    for q in event.questions:
                        answers.update(
                            await asyncio.to_thread(_ask_single_question, q)
                        )
                    assert event.future is not None
                    event.future.set_result(answers)
                asyncio.create_task(_resolve_questions())
            else:
                event.future.set_result({})
        else:
            base_handler(event)

    return handler
