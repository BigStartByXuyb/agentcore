"""Unified tool execution entry point (async).

Corresponds to Claude Code's toolExecution.ts runToolUse() — the single
place where executor() and map_result() are called, wrapped in try/catch
so tool failures never crash the agent loop.

Supports two executor shapes:
  - async def executor(...) -> ToolResult   (bash, read_file, grep)
      Returns a coroutine.  Runner awaits it.
  - def executor(...) -> AsyncGenWithResult (agent, fork skill)
      Returns an AsyncGenWithResult.  Runner iterates .events() then
      reads .result.

run_tool_use() itself returns an AsyncGenWithResult so the caller can
iterate sub-agent events AND recover the terminal (ToolResult, str, bool).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import AsyncIterator

from src.types import AsyncGenWithResult, ToolResult, ToolUseContext, ToolCall, ToolCallGroup
from src.tools import ALL_TOOLS
from src.events import AgentEvent
from src.events import ToolStart, ToolEnd

logger = logging.getLogger(__name__)

ToolUseReturn = tuple[ToolResult, str, str, bool]

def merge_tool_call(id:str, tool_name: str, tool_input: dict, groups: list[ToolCallGroup]) -> None:
    """Group consecutive tool calls by read-only/read-write type."""
    tool = ALL_TOOLS[tool_name]
    call_type = "read-only" if tool.is_read_only(tool_input) else "read-write"

    if groups and groups[-1].type == call_type:
        groups[-1].tool_call.append(ToolCall(id=id, name=tool_name, input=tool_input))
    else:
        groups.append(ToolCallGroup(tool_call=[ToolCall(id=id, name=tool_name, input=tool_input)], type=call_type))


def run_tool_use(
    label:str,
    id:str,
    tool_name: str,
    tool_input: dict,
    context: ToolUseContext,
) -> AsyncGenWithResult[AgentEvent, ToolUseReturn]:
    """Execute a single tool, yielding events and producing a result.

    Returns an AsyncGenWithResult that:
      - yields AgentEvent objects from generator-based executors
      - sets .result to (ToolResult, llm_text, is_error)
    """
    if tool_name not in ALL_TOOLS:
        return AsyncGenWithResult.of_value(
            (ToolResult(data=None), f"No such tool: '{tool_name}'", True)
        )

    if context.tool_overrides and tool_name in context.tool_overrides:
        tool = context.tool_overrides[tool_name]
    elif tool_name not in context.tools:
        return AsyncGenWithResult.of_value(
            (ToolResult(data=None), f"Tool '{tool_name}' is not available in current context", True)
        )
    else:
        tool = ALL_TOOLS[tool_name]

    _tool = tool

    async def _impl(run: AsyncGenWithResult) -> AsyncIterator[AgentEvent]:
        try:
            yield ToolStart(label=label,tool_name=tool_name, tool_input=tool_input)
            ret = _tool.executor(tool_input, context)

            if isinstance(ret, AsyncGenWithResult):
                async for ev in ret.events():
                    yield ev
                result = ret.result
            elif asyncio.iscoroutine(ret) or inspect.isawaitable(ret):
                result = await ret
            else:
                result = ret

        except Exception as e:
            error_text = f"Tool '{tool_name}' executor failed: {type(e).__name__}: {e}"
            logger.error(error_text, exc_info=True)
            run.set_result((ToolResult(data=None), id, error_text, True))
            yield ToolEnd(label=label,is_error=True, tool_name=tool_name, result_summary=error_text)
            return

        try:
            llm_text = _tool.map_result(result.data)
        except Exception as e:
            error_text = f"Tool '{tool_name}' map_result failed: {type(e).__name__}: {e}"
            logger.error(error_text, exc_info=True)
            run.set_result((ToolResult(data=None), id, error_text, True))
            yield ToolEnd(label=label,is_error=True, tool_name=tool_name, result_summary=error_text)
            return
        yield ToolEnd(label=label,is_error=False, tool_name=tool_name, result_summary=llm_text)
        run.set_result((result, id, llm_text, False))

    return AsyncGenWithResult(_impl)


def execute_tool_groups(
    label:str,
    groups: list[ToolCallGroup],
    context: ToolUseContext,
) -> AsyncGenWithResult[AgentEvent, list[ToolUseReturn]]:
    """Batch execute tool groups with concurrency control.

    - read-only groups: concurrent via asyncio.gather, events replayed after completion
    - read-write groups: sequential with real-time event bubbling
    """

    async def _impl(run: AsyncGenWithResult) -> AsyncIterator[AgentEvent]:
        all_results: list[ToolUseReturn] = []

        for group in groups:
            if group.type == "read-only" and len(group.tool_call) > 1:
                async def _drain(call: ToolCall) -> tuple[ToolUseReturn, list[AgentEvent]]:
                    r = run_tool_use(label, call.id, call.name, call.input, context)
                    events: list[AgentEvent] = []
                    async for ev in r.events():
                        events.append(ev)
                    return r.result, events

                gathered = await asyncio.gather(
                    *[_drain(c) for c in group.tool_call]
                )
                for result, events in gathered:
                    for ev in events:
                        yield ev
                    all_results.append(result)
            else:
                for call in group.tool_call:
                    r = run_tool_use(label, call.id, call.name, call.input, context)
                    async for ev in r.events():
                        yield ev
                    all_results.append(r.result)

        run.set_result(all_results)

    return AsyncGenWithResult(_impl)
