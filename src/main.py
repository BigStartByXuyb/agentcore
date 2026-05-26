"""CLI entry point for the agent (async)."""

import asyncio
import io
import sys

# Fix Windows terminal encoding — stdout/stderr need UTF-8 so LLM output
# (which may contain unicode) renders correctly instead of crashing on GBK.
# Do NOT replace stdin: input() relies on the original console handle for
# echo and line editing. Replacing it with TextIOWrapper breaks both.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace"
    )

from src.core import config
from src.agent_loop import agent_loop
from src.core.types import AgentState, MessageHistory, PlanPhase
from src.utils.file_state_cache import FileStateCache
from src.mcp_tool import register_mcp_tools, shutdown_mcp
from src.tools import registry as tool_registry
from src.watcher import start_watchers


async def async_main() -> None:
    if not config.ANTHROPIC_AUTH_TOKEN:
        print("Error: ANTHROPIC_AUTH_TOKEN environment variable is not set.")
        sys.exit(1)

    from src.sandbox import sandbox_manager
    from src.memory.paths import ensure_memory_dir
    ensure_memory_dir()
    print(f"my-agent ready. {sandbox_manager.status_summary()}")
    print("Type 'exit' to quit.\n")

    history = MessageHistory()
    state = AgentState()
    file_cache = FileStateCache()
    await register_mcp_tools(tool_registry)
    start_watchers(asyncio.get_running_loop())

    try:
        while True:
            try:
                user_input = await asyncio.to_thread(input, "> ")
            except (EOFError, KeyboardInterrupt):
                print("\nBye.")
                break

            stripped = user_input.strip()
            if not stripped:
                continue
            if stripped in ("exit", "quit"):
                print("Bye.")
                break

            # --- /clear command: reset session state ---
            if stripped == "/clear":
                from src.plan_mode import clear_slug_cache
                clear_slug_cache(state.agent_id)
                if hasattr(state, "_task_store") and state._task_store is not None:
                    state._task_store.clear()
                history = MessageHistory()
                state = AgentState()
                file_cache = FileStateCache()
                print("Context cleared.")
                continue

            # --- /plan command: toggle plan mode ---
            if stripped == "/plan" or stripped.startswith("/plan "):
                task_desc = stripped[5:].strip() if len(stripped) > 5 else ""
                if state.plan_phase == PlanPhase.ACTIVE and not task_desc:
                    state.plan_phase = PlanPhase.EXITING
                    print("Plan mode deactivated.")
                    continue
                if state.plan_phase != PlanPhase.ACTIVE:
                    from src.plan_mode import enter_plan_mode
                    state.plan_file_path = enter_plan_mode(session_id=state.agent_id)
                    state.plan_phase = PlanPhase.ACTIVE
                    print(f"Plan mode activated. Plan file: {state.plan_file_path}")
                if not task_desc:
                    continue
                stripped = task_desc

            try:
                await agent_loop(stripped, history, state, file_cache)
            except Exception as e:
                print(f"\n[Error] {e}")
    finally:
        await shutdown_mcp()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
