"""Unified tool execution entry point (async, callback-based).

The single place where executor() and map_result() are called, wrapped in
try/catch so tool failures never crash the agent loop.

All tool executors are now `async def executor(inputs, ctx) -> ToolResult`.
Tools that need to emit events use `ctx.on_event` callback.

Event output uses the `on_event` callback pattern instead of generators.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, replace

from src.core.types import EventCallback, ToolDef, ToolPermissionResult, ToolResult, ToolUseContext, ToolCall, ToolCallGroup
from src.tools import registry as tool_registry
from src.core.events import ToolStart, ToolEnd, PermissionRequest, PermissionDenied

logger = logging.getLogger(__name__)

ToolUseReturn = tuple[ToolResult, str, str, bool]

def merge_tool_call(id:str, tool_name: str, tool_input: dict, groups: list[ToolCallGroup]) -> None:
    """Group consecutive tool calls by read-only/read-write type."""
    tool = tool_registry.get(tool_name)
    if tool is None:
        return
    try:
        is_ro = tool.is_read_only(tool_input)
    except Exception:
        is_ro = False
    call_type = "read-only" if is_ro else "read-write"

    if groups and groups[-1].type == call_type:
        groups[-1].tool_call.append(ToolCall(id=id, name=tool_name, input=tool_input))
    else:
        groups.append(ToolCallGroup(tool_call=[ToolCall(id=id, name=tool_name, input=tool_input)], type=call_type))


async def run_tool_use(
    label: str,
    id: str,
    tool_name: str,
    tool_input: dict,
    context: ToolUseContext,
    on_event: EventCallback,
) -> ToolUseReturn:
    """Execute a single tool, emitting events via on_event callback.

    Returns (ToolResult, tool_use_id, llm_text, is_error).
    """
    if tool_name not in tool_registry.list_names():
        return (ToolResult(data=None), id, f"No such tool: '{tool_name}'", True)

    if context.tool_overrides and tool_name in context.tool_overrides:
        tool = context.tool_overrides[tool_name]
    elif tool_name not in context.tools:
        return (ToolResult(data=None), id, f"Tool '{tool_name}' is not available in current context", True)
    else:
        resolved = tool_registry.get(tool_name)
        assert resolved is not None
        tool = resolved

    try:
        on_event(ToolStart(label=label, tool_name=tool_name, tool_input=tool_input))

        # --- Permission check ---
        perm = check_tool_permissions(tool, tool_name, tool_input, context)

        if perm.behavior == "deny":
            on_event(PermissionDenied(label=label, tool_name=tool_name, message=perm.deny_message))
            on_event(ToolEnd(label=label, is_error=True, tool_name=tool_name, result_summary=perm.deny_message))
            if context.denial_tracker is not None:
                context.denial_tracker.record_denial()
            return (ToolResult(data=None), id, perm.deny_message, True)

        if perm.behavior == "ask":
            preview: str | None = None
            if tool.build_preview is not None:
                try:
                    preview = tool.build_preview(tool_input)
                except Exception:
                    pass

            future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
            on_event(PermissionRequest(
                label=label, tool_name=tool_name, tool_input=tool_input,
                future=future, ask_mode=perm.ask_mode,
                custom_prompt=perm.custom_prompt, preview=preview,
            ))
            answer = await future

            if answer in ("y", "yes"):
                pass
            elif answer == "always" and perm.ask_mode == "standard" and context.permissions is not None:
                from src.permissions import PermissionRule
                context.permissions.add_session_rule(PermissionRule(
                    tool_name=tool_name,
                    content_pattern=perm.engine_content,
                    behavior="allow",
                    source="session",
                ))
            else:
                if perm.ask_mode == "review":
                    feedback = answer[2:] if answer.startswith("n:") else ""
                    deny_msg = "User rejected the plan."
                    if feedback:
                        deny_msg += f" Feedback: {feedback}"
                else:
                    deny_msg = f"User denied permission for {tool_name}."
                on_event(ToolEnd(label=label, is_error=True, tool_name=tool_name, result_summary=deny_msg))
                return (ToolResult(data=None), id, deny_msg, True)

        ctx_with_event = replace(context, on_event=on_event)
        ret = tool.executor(tool_input, ctx_with_event)

        if asyncio.iscoroutine(ret) or inspect.isawaitable(ret):
            result = await ret
        else:
            result = ret

        if context.denial_tracker is not None:
            context.denial_tracker.record_success()

    except asyncio.CancelledError:
        error_text = f"Tool '{tool_name}' was cancelled."
        on_event(ToolEnd(label=label, is_error=True, tool_name=tool_name, result_summary=error_text))
        return (ToolResult(data=None), id, error_text, True)

    except Exception as e:
        error_text = f"Tool '{tool_name}' executor failed: {type(e).__name__}: {e}"
        logger.error(error_text, exc_info=True)
        on_event(ToolEnd(label=label, is_error=True, tool_name=tool_name, result_summary=error_text))
        return (ToolResult(data=None), id, error_text, True)

    try:
        llm_text = tool.map_result(result.data)
    except Exception as e:
        error_text = f"Tool '{tool_name}' map_result failed: {type(e).__name__}: {e}"
        logger.error(error_text, exc_info=True)
        on_event(ToolEnd(label=label, is_error=True, tool_name=tool_name, result_summary=error_text))
        return (ToolResult(data=None), id, error_text, True)

    display_text = ""
    if tool.display_result is not None:
        try:
            display_text = tool.display_result(result.data)
        except Exception:
            display_text = ""

    on_event(ToolEnd(label=label, is_error=False, tool_name=tool_name, result_summary=llm_text, display_text=display_text))
    return (result, id, llm_text, False)


async def execute_tool_groups(
    label: str,
    groups: list[ToolCallGroup],
    context: ToolUseContext,
    on_event: EventCallback,
) -> list[ToolUseReturn]:
    """Batch execute tool groups with concurrency control.

    - read-only groups: concurrent via asyncio.gather (events may interleave)
    - read-write groups: sequential with direct on_event calls
    """
    all_results: list[ToolUseReturn] = []

    for group in groups:
        if group.type == "read-only" and len(group.tool_call) > 1:
            # Concurrent execution — pass on_event directly, output may interleave.
            created_tasks = [
                asyncio.create_task(
                    run_tool_use(label, c.id, c.name, c.input, context, on_event)
                )
                for c in group.tool_call
            ]
            try:
                results = await asyncio.gather(*created_tasks)
            except BaseException:
                for t in created_tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*created_tasks, return_exceptions=True)
                raise
            all_results.extend(results)

            # --- Ordered output version (Queue buffering) ---
            # If ordered output is needed, uncomment below and comment out the gather above.
            #
            # _SENTINEL = object()
            # queues: list[asyncio.Queue] = []
            # results: list[ToolUseReturn] = [None] * len(group.tool_call)  # type: ignore
            #
            # async def _produce(idx: int, call: ToolCall, q: asyncio.Queue) -> None:
            #     def _task_on_event(ev):
            #         q.put_nowait(ev)
            #     r = await run_tool_use(label, call.id, call.name, call.input, context, _task_on_event)
            #     results[idx] = r
            #     await q.put(_SENTINEL)
            #
            # for i, c in enumerate(group.tool_call):
            #     q: asyncio.Queue = asyncio.Queue()
            #     queues.append(q)
            #     asyncio.create_task(_produce(i, c, q))
            #
            # for i, q in enumerate(queues):
            #     while True:
            #         ev = await q.get()
            #         if ev is _SENTINEL:
            #             break
            #         on_event(ev)
            #     all_results.append(results[i])
        else:
            for call in group.tool_call:
                r = await run_tool_use(label, call.id, call.name, call.input, context, on_event)
                all_results.append(r)

    return all_results


# ---------------------------------------------------------------------------
# StreamingToolExecutor — execute tools as they stream in
# ---------------------------------------------------------------------------

@dataclass
class _TrackedTool:
    id: str
    name: str
    input: dict
    is_concurrent_safe: bool
    status: str  # "queued" | "executing" | "completed"
    task: asyncio.Task | None = None
    result: ToolUseReturn | None = None


class StreamingToolExecutor:
    """Execute tools as they arrive during API streaming.

    - Concurrent-safe tools (read-only) run in parallel as independent tasks
    - Non-concurrent tools queue behind all preceding tools
    - Events are emitted directly via on_event (may interleave for concurrent tools)
    """

    def __init__(self, label: str, context: ToolUseContext, on_event: EventCallback) -> None:
        self._label = label
        self._context = context
        self._on_event = on_event
        self._tools: list[_TrackedTool] = []

    def add_tool(self, block) -> None:
        tool_def = tool_registry.get(block.name)
        is_safe = False
        if tool_def is not None:
            try:
                is_safe = tool_def.is_read_only(
                    block.input if isinstance(block.input, dict) else {}
                )
            except Exception:
                is_safe = False

        tracked = _TrackedTool(
            id=block.id,
            name=block.name,
            input=block.input if isinstance(block.input, dict) else {},
            is_concurrent_safe=is_safe,
            status="queued",
        )
        self._tools.append(tracked)
        self._maybe_start()

    def has_tools(self) -> bool:
        return len(self._tools) > 0

    def discard(self) -> None:
        """Cancel all in-flight tool tasks and clear the tool list.

        Called when streaming fallback occurs — partial results from the
        failed stream are invalid and must not be used.
        """
        for tracked in self._tools:
            if tracked.task is not None and not tracked.task.done():
                tracked.task.cancel()
        self._tools.clear()

    def _maybe_start(self) -> None:
        for tracked in self._tools:
            if tracked.status != "queued":
                continue

            executing = [t for t in self._tools if t.status == "executing"]
            can_run = (
                len(executing) == 0
                or (tracked.is_concurrent_safe and all(t.is_concurrent_safe for t in executing))
            )

            if can_run:
                tracked.status = "executing"
                tracked.task = asyncio.create_task(self._run_one(tracked))
            elif not tracked.is_concurrent_safe:
                break

    async def _run_one(self, tracked: _TrackedTool) -> None:
        try:
            result = await run_tool_use(
                self._label, tracked.id, tracked.name, tracked.input,
                self._context, self._on_event,
            )
            tracked.result = result
        finally:
            tracked.status = "completed"
            self._maybe_start()

    async def drain_completed(self) -> None:
        """No-op: events are emitted directly via on_event during execution."""
        pass

        # --- Ordered output version (Queue buffering) ---
        # If ordered output is needed, add event_queue/drained fields to _TrackedTool,
        # wrap on_event with q.put_nowait in _run_one, then uncomment:
        #
        # for tracked in self._tools:
        #     if tracked.drained:
        #         continue
        #     while not tracked.event_queue.empty():
        #         ev = tracked.event_queue.get_nowait()
        #         if ev is _SENTINEL:
        #             tracked.drained = True
        #             break
        #         self._on_event(ev)
        #     if not tracked.drained and tracked.status == "executing":
        #         break

    async def drain_remaining(self) -> None:
        """Wait for all tool tasks to complete, including ones started by _maybe_start
        in a finishing task's finally block. Loops until no active tasks remain."""
        while True:
            active = [t.task for t in self._tools
                      if t.task is not None and not t.task.done()]
            if not active:
                return
            await asyncio.gather(*active, return_exceptions=True)

        # --- Ordered output version (Queue buffering) ---
        # for tracked in self._tools:
        #     if tracked.drained:
        #         continue
        #     while True:
        #         ev = await tracked.event_queue.get()
        #         if ev is _SENTINEL:
        #             tracked.drained = True
        #             break
        #         self._on_event(ev)

    def collect_results(self) -> list[ToolUseReturn]:
        return [t.result for t in self._tools if t.result is not None]


# ---------------------------------------------------------------------------
# Denial tracking — prevents infinite retry loops on permission denial
# ---------------------------------------------------------------------------

MAX_CONSECUTIVE_DENIALS = 3
MAX_TOTAL_DENIALS = 20


class DenialTracker:
    """Tracks consecutive and total tool permission denials.

    In interactive mode (headless=False): purely informational counting.
    The loop never breaks — LLM sees the denial message and decides.

    In headless mode (headless=True): sets abort_requested after limits
    are exceeded, causing the agent loop to terminate. This prevents
    infinite retry loops in unattended/CI environments.
    """

    def __init__(self, headless: bool = False) -> None:
        self.consecutive: int = 0
        self.total: int = 0
        self.headless: bool = headless
        self.abort_requested: bool = False
        self.abort_message: str = ""

    def record_denial(self) -> None:
        self.consecutive += 1
        self.total += 1
        if not self.headless:
            return
        if self.consecutive >= MAX_CONSECUTIVE_DENIALS:
            self.abort_requested = True
            self.abort_message = (
                f"Too many consecutive permission denials ({self.consecutive}). Aborting agent."
            )
        elif self.total >= MAX_TOTAL_DENIALS:
            self.abort_requested = True
            self.abort_message = (
                f"Too many total permission denials ({MAX_TOTAL_DENIALS}). Aborting agent."
            )

    def record_success(self) -> None:
        self.consecutive = 0


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------

def check_tool_permissions(
    tool: ToolDef, tool_name: str, tool_input: dict, context: ToolUseContext,
) -> ToolPermissionResult:
    """Resolve permission: tool.check_permissions first, PermissionEngine as passthrough fallback.

      1. tool.check_permissions() → allow/ask/deny → use directly
      2. tool.check_permissions() → passthrough → PermissionEngine.check()
    """
    check_result = tool.check_permissions(tool_input, context)

    if check_result.behavior != "passthrough":
        return check_result

    if context.permissions is not None:
        content = _extract_content(tool_name, tool_input)
        result = context.permissions.check(tool_name, content)
        result.engine_content = content

        if result.behavior == "deny":
            if not result.deny_message:
                result.deny_message = f"Permission denied for {tool_name}."
            return result

        # No rule matched → ask. Sub-agent cannot prompt, convert to deny.
        if result.behavior == "ask" and context.permissions._headless:
            return ToolPermissionResult(
                behavior="deny",
                deny_message=f"Permission denied for {tool_name} (sub-agent cannot prompt).",
            )

        return result

    return ToolPermissionResult(behavior="ask")


def _extract_content(tool_name: str, tool_input: dict) -> str | None:
    """Extract the permission-relevant content string from tool input."""
    if tool_name == "bash":
        return tool_input.get("command")
    if tool_name in ("read_file", "write_file", "edit_file"):
        return tool_input.get("file_path")
    return None
