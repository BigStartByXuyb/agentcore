"""Unified tool execution entry point.

Corresponds to Claude Code's toolExecution.ts runToolUse() — the single
place where executor() and map_result() are called, wrapped in try/catch
so tool failures never crash the agent loop.

Supports both regular executors (return ToolResult) and generator executors
(yield AgentEvent, return ToolResult via StopIteration.value).  Generator
executors are used by tools that spawn sub-agents (agent tool, fork skill)
so their events can bubble up to the parent via `yield from`.
"""

from __future__ import annotations

import logging
from typing import Generator

from src.types import ToolResult, ToolUseContext
from src.tools import ALL_TOOLS
from src.events import AgentEvent

logger = logging.getLogger(__name__)


def run_tool_use(
    tool_name: str,
    tool_input: dict,
    tool_use_id: str,
    context: ToolUseContext,
) -> Generator[AgentEvent, None, tuple[ToolResult, str, bool]]:
    """Look up tool by name, execute, and map result to LLM-readable text.

    This is a generator: it yields AgentEvent objects from generator-based
    executors (via `yield from`), and returns (ToolResult, llm_text, is_error)
    via StopIteration.value.

    Regular executors (bash, read_file, grep) return ToolResult directly —
    no events are yielded.  Generator executors (agent, fork skill) yield
    sub-agent events that bubble up through the chain.
    """
    # 1. Check tool exists in global registry
    if tool_name not in ALL_TOOLS:
        return ToolResult(data=None), f"No such tool: '{tool_name}'", True

    # 2. Use sandboxed version if override exists
    if context.tool_overrides and tool_name in context.tool_overrides:
        tool = context.tool_overrides[tool_name]
    # 3. Check tool is in the allowed list
    elif tool_name not in context.tools:
        return ToolResult(data=None), f"Tool '{tool_name}' is not available in current context", True
    # 4. Allowed — use global version
    else:
        tool = ALL_TOOLS[tool_name]

    # --- executor ---
    try:
        result_or_gen = tool.executor(tool_input, context)
        # Generator executor — yield from to bubble sub-agent events
        if hasattr(result_or_gen, '__next__'):
            result = yield from result_or_gen
        else:
            result = result_or_gen
    except Exception as e:
        error_text = f"Tool '{tool_name}' executor failed: {type(e).__name__}: {e}"
        logger.error(error_text, exc_info=True)
        return ToolResult(data=None), error_text, True

    # --- map_result ---
    try:
        llm_text = tool.map_result(result.data)
    except Exception as e:
        error_text = f"Tool '{tool_name}' map_result failed: {type(e).__name__}: {e}"
        logger.error(error_text, exc_info=True)
        return ToolResult(data=None), error_text, True

    return result, llm_text, False
