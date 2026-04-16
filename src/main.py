"""CLI entry point for the agent."""

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

from src import config
from src.agent_loop import agent_loop
from src.types import AgentState, MessageHistory


def main() -> None:
    if not config.ANTHROPIC_AUTH_TOKEN:
        print("Error: ANTHROPIC_AUTH_TOKEN environment variable is not set.")
        sys.exit(1)

    print("my-agent ready. Type 'exit' to quit.\n")

    # Persistent conversation state — survives across user turns.
    # Mirrors Claude Code's REPL which owns the messages array and
    # passes it into queryLoop() each turn.
    history = MessageHistory()
    state = AgentState()

    while True:
        try:
            user_input = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        stripped = user_input.strip()
        if not stripped:
            continue
        if stripped in ("exit", "quit"):
            print("Bye.")
            break

        try:
            # agent_loop() uses streaming — text is already printed to
            # the terminal in real-time via the on_text callback inside
            # run_agent_loop().  We only need the return value for
            # potential programmatic use; printing it again would
            # duplicate the output.
            agent_loop(stripped, history, state)
        except Exception as e:
            print(f"\n[Error] {e}")


if __name__ == "__main__":
    main()
