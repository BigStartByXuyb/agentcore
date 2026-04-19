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

from src.types import AsyncGenWithResult, ToolResult, ToolUseContext
from src.tools import ALL_TOOLS
from src.events import AgentEvent

logger = logging.getLogger(__name__)

ToolUseReturn = tuple[ToolResult, str, bool]


def run_tool_use(
    tool_name: str,
    tool_input: dict,
    tool_use_id: str,
    context: ToolUseContext,
) -> AsyncGenWithResult[AgentEvent, ToolUseReturn]:
    """Look up tool by name, execute, and map result to LLM-readable text.

    Returns an AsyncGenWithResult that:
      - yields AgentEvent objects from generator-based executors
      - sets .result to (ToolResult, llm_text, is_error)

    Regular executors (bash, read_file, grep) are coroutines — no events
    are yielded.  Generator executors (agent, fork skill) return an
    AsyncGenWithResult whose events bubble up through the chain.
    """
    # 1. Check tool exists in global registry
    if tool_name not in ALL_TOOLS:
        return AsyncGenWithResult.of_value(
            (ToolResult(data=None), f"No such tool: '{tool_name}'", True)
        )

    # 2. Use sandboxed version if override exists
    if context.tool_overrides and tool_name in context.tool_overrides:
        tool = context.tool_overrides[tool_name]
    # 3. Check tool is in the allowed list
    elif tool_name not in context.tools:
        return AsyncGenWithResult.of_value(
            (ToolResult(data=None), f"Tool '{tool_name}' is not available in current context", True)
        )
    # 4. Allowed — use global version
    else:
        tool = ALL_TOOLS[tool_name]

    # Capture tool ref for the async impl closure
    _tool = tool

    async def _impl(run: AsyncGenWithResult) -> AsyncIterator[AgentEvent]:
        # --- executor ---
        try:
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
            run.set_result((ToolResult(data=None), error_text, True))
            return

        # --- map_result ---
        try:
            llm_text = _tool.map_result(result.data)
        except Exception as e:
            error_text = f"Tool '{tool_name}' map_result failed: {type(e).__name__}: {e}"
            logger.error(error_text, exc_info=True)
            run.set_result((ToolResult(data=None), error_text, True))
            return

        run.set_result((result, llm_text, False))

    return AsyncGenWithResult(_impl)
