"""System prompt construction.

The system prompt is kept stable and minimal so it benefits from
Anthropic's prompt caching.  Dynamic per-turn content (skill listings,
CLAUDE.md reminders, etc.) is injected as user messages — see
agent_loop.py for the injection point.

The memory behavioral instructions are appended here (stable across
turns) while selected memory *content* is injected per-turn as
<system-reminder> user messages by agent_loop.py.
"""

import os

from src.memory.prompt import build_memory_prompt
from src.memory.paths import get_memory_dir


def build_system_prompt() -> str:
    """Build the static system prompt.

    Mirrors Claude Code's systemPrompt.ts — the base prompt is constant
    across turns so the API can cache it.  Skill listings are NOT included
    here; they are injected as a <system-reminder> user message by the
    agent loop on the first turn (see build_skill_reminder()).
    """
    cwd = os.getcwd()
    base = (
        "You are an AI assistant with access to tools for interacting with "
        "the local filesystem and running commands.\n"
        "Use the tools provided to help the user accomplish their tasks.\n"
        f"\nWorking directory: {cwd}\n"
        "Platform: " + os.name + "\n"
    )

    # Append memory behavioral instructions (stable, cacheable)
    memory_section = build_memory_prompt(get_memory_dir())
    if memory_section:
        base += "\n\n# Memory\n\n" + memory_section

    return base
