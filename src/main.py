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
from src.core.types import AgentState, MessageHistory
from src.utils.file_state_cache import FileStateCache
from src.mcp_tool import register_mcp_tools, shutdown_mcp
from src.tools import registry as tool_registry
from src.watcher import start_watchers
from src.session import SessionStorage
from src.commands import CommandContext, handle_clear, handle_resume, handle_sessions, handle_plan, handle_settings


def _parse_resume_arg() -> str | None:
    """Parse --resume [session_id] from sys.argv."""
    if "--resume" not in sys.argv:
        return None
    idx = sys.argv.index("--resume")
    if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("-"):
        return sys.argv[idx + 1]
    return SessionStorage.last_session_id()


async def async_main() -> None:
    from src.sandbox import sandbox_manager
    from src.memory.paths import ensure_memory_dir
    from src.core.settings import ensure_default_settings, validate_config

    ensure_default_settings()
    ensure_memory_dir()

    errors = validate_config()
    if errors:
        for e in errors:
            print(f"[Config Error] {e}")
        sys.exit(1)
    print(f"my-agent ready. {sandbox_manager.status_summary()}")
    print("Type 'exit' to quit.\n")

    # --- Resume or fresh session ---
    resume_id = _parse_resume_arg()
    if resume_id:
        try:
            history = SessionStorage.load(resume_id)
            print(f"[Resumed session: {resume_id}, {len(history)} messages]")
        except FileNotFoundError:
            print(f"[Session '{resume_id}' not found, starting fresh]")
            history = MessageHistory()
            resume_id = None
    else:
        history = MessageHistory()

    state = AgentState()
    file_cache = FileStateCache()
    await register_mcp_tools(tool_registry)
    start_watchers(asyncio.get_running_loop())

    # --- Session storage ---
    storage: SessionStorage | None = None
    if config.get().session_persist_enabled:
        storage = SessionStorage(config.SESSION_ID)
        storage.open()

    ctx = CommandContext(history=history, state=state, file_cache=file_cache, storage=storage)

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

            # --- Slash commands ---
            if stripped == "/clear":
                ctx = await handle_clear(ctx)
                continue

            if stripped == "/resume" or stripped.startswith("/resume "):
                arg = stripped[7:].strip() if len(stripped) > 7 else ""
                ctx = await handle_resume(ctx, arg)
                continue

            if stripped == "/sessions":
                ctx = await handle_sessions(ctx)
                continue

            if stripped == "/settings" or stripped.startswith("/settings "):
                arg = stripped[9:].strip() if len(stripped) > 9 else ""
                ctx = await handle_settings(ctx, arg)
                continue

            if stripped == "/plan" or stripped.startswith("/plan "):
                task_desc = stripped[5:].strip() if len(stripped) > 5 else ""
                ctx, forwarded = handle_plan(ctx, task_desc)
                if forwarded is None:
                    continue
                stripped = forwarded

            # --- Agent loop ---
            try:
                await agent_loop(stripped, ctx.history, ctx.state, ctx.file_cache, session_storage=ctx.storage)
            except Exception as e:
                print(f"\n[Error] {e}")
    finally:
        if ctx.storage:
            ctx.storage.close()
        await shutdown_mcp()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
