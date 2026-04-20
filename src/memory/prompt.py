"""Build memory behavioral instructions for the system prompt.

Corresponds to Claude Code's src/memdir/memdir.ts:
  - buildMemoryLines()  → _build_behavioral_instructions()
  - buildMemoryPrompt() → build_memory_prompt()
  - loadMemoryPrompt()  → build_memory_prompt() (combined)

The behavioral instructions tell the LLM:
  - What memory types exist and when to save each
  - The frontmatter file format
  - Two-step save process (write file + update MEMORY.md)
  - When to access memories
  - What NOT to save
"""

from __future__ import annotations

import os
import logging

from src import config
from src.memory.paths import get_memory_dir, get_memory_dir_display, get_memory_entrypoint, ensure_memory_dir

logger = logging.getLogger(__name__)

# Max lines to read from MEMORY.md before truncation
_MAX_ENTRYPOINT_LINES = 200


def build_memory_prompt(memory_dir: str | None = None) -> str | None:
    """Build the STATIC memory rules for the system prompt.

    Returns None if memory is disabled.  Otherwise returns only the
    behavioral instructions (types, rules, format, tag explanations).

    The dynamic content (MEMORY.md index + recalled memory files) is
    injected per-turn as a <memory-context> user message by agent_loop.py,
    keeping the system prompt stable and maximising prompt cache hits.
    """
    if not config.MEMORY_ENABLED:
        return None

    mem_dir = memory_dir or get_memory_dir()
    ensure_memory_dir()

    return _build_behavioral_instructions(mem_dir)


def build_memory_user_message(memory_dir: str | None = None) -> str | None:
    """Load the MEMORY.md index content for per-turn injection.

    Returns the raw index text, or None if memory is disabled or
    there is no index content.  The caller (_inject_memory_context
    in agent_loop.py) wraps this in <memory-index> tags.
    """
    if not config.MEMORY_ENABLED:
        return None

    mem_dir = memory_dir or get_memory_dir()
    return _load_entrypoint(mem_dir)


def _build_behavioral_instructions(memory_dir: str) -> str:
    """Core behavioral instructions — tells the LLM how to use memory."""
    display_dir = get_memory_dir_display()
    return f"""You have a persistent, file-based memory system at `{display_dir}`.

## Memory Types

There are 4 types of memory:

- **user**: Information about the user's role, preferences, and knowledge. Helps tailor responses.
- **feedback**: Guidance the user has given about how to approach work — corrections AND confirmations.
- **project**: Ongoing work context, goals, decisions not derivable from code/git.
- **reference**: Pointers to external resources (URLs, tool locations, dashboards).

## How to Save Memories

Two-step process:

**Step 1** — Write the memory to its own file (e.g. `user_role.md`) with frontmatter:

```markdown
---
name: {{memory name}}
description: {{one-line description}}
type: {{user|feedback|project|reference}}
---

{{memory content}}
```

**Step 2** — Add a pointer to MEMORY.md (the index). Each entry is one line under ~150 chars:
`- [Title](file.md) — one-line hook`

## When to Access Memories

- When memories seem relevant to the current task
- When the user explicitly asks you to check, recall, or remember
- If the user says to ignore memory: do not apply, cite, or mention memory content

## What NOT to Save

- Code patterns, architecture, file paths — derivable from reading the project
- Git history — use git log/blame
- Debugging solutions — the fix is in the code
- Ephemeral task details or current conversation context

## Dynamic Tags (injected per-turn in user messages)

Each turn, the following tags may appear in a user message:

- `<memory-index>` — current MEMORY.md index content (always present, up to {_MAX_ENTRYPOINT_LINES} lines)
- `<memory-recalled>` — full content of memory files selected as relevant to the current query (async, may arrive after first LLM call)

These tags are system-injected, not user-written. Use the index to know what memories exist;
use recalled content as context for your response.

## Rules

- Do not write duplicate memories — check existing ones first
- Update or remove memories that become outdated
- Convert relative dates to absolute dates when saving"""


def _load_entrypoint(memory_dir: str) -> str | None:
    """Read MEMORY.md content, truncating at _MAX_ENTRYPOINT_LINES."""
    entrypoint = get_memory_entrypoint()
    if not os.path.isfile(entrypoint):
        return None

    try:
        with open(entrypoint, "r", encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= _MAX_ENTRYPOINT_LINES:
                    lines.append(f"\n... (truncated at {_MAX_ENTRYPOINT_LINES} lines)")
                    break
                lines.append(line)
        content = "".join(lines).strip()
        return content if content else None
    except OSError as e:
        logger.debug("Failed to read MEMORY.md: %s", e)
        return None
