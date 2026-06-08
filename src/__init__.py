"""my-agent public API.

High-level usage (manages reminders, memory, permissions)::

    import asyncio
    from src import init, agent_loop, MessageHistory, AgentState, make_headless_handler

    async def main():
        await init()
        history = MessageHistory()
        state = AgentState()
        result = await agent_loop(
            "Hello", history, state,
            on_event=make_headless_handler(),
        )
        print(result.text)

    asyncio.run(main())

Low-level usage (caller owns ToolUseContext, no auto-reminders/memory)::

    from src import init, run_agent_loop, ToolUseContext, MessageHistory, make_headless_handler

    async def main():
        await init()
        history = MessageHistory()
        history.add_user("Hello")
        ctx = ToolUseContext(
            messages=history,
            tools=["bash", "read_file"],
            system_prompt="You are a helpful assistant.",
            label="custom",
        )
        result = await run_agent_loop(
            tool_use_context=ctx,
            max_turns=10,
            on_event=make_headless_handler(),
        )
        print(result.text)

    asyncio.run(main())
"""

from src.agent_loop import agent_loop, run_agent_loop, init
from src.core.types import MessageHistory, AgentState, LoopResult, EventCallback, ToolUseContext
from src.core.events import (
    AgentEvent,
    TextDelta,
    ThinkingDelta,
    TextBlock,
    ThinkingBlock,
    ToolStart,
    ToolEnd,
    TokenUsage,
    ErrorEvent,
    RetryNotice,
)
from src.display import (
    default_handler,
    make_interactive_handler,
    make_headless_handler,
    make_allow_all_handler,
)

__all__ = [
    "init",
    "agent_loop",
    "run_agent_loop",
    "MessageHistory",
    "AgentState",
    "LoopResult",
    "EventCallback",
    "ToolUseContext",
    "AgentEvent",
    "TextDelta",
    "ThinkingDelta",
    "TextBlock",
    "ThinkingBlock",
    "ToolStart",
    "ToolEnd",
    "TokenUsage",
    "ErrorEvent",
    "RetryNotice",
    "default_handler",
    "make_interactive_handler",
    "make_headless_handler",
    "make_allow_all_handler",
]
