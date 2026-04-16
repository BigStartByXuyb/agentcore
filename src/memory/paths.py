"""Memory directory path resolution and existence checks.

Corresponds to Claude Code's src/memdir/paths.ts:
  - getAutoMemPath()       → get_memory_dir()
  - getAutoMemEntrypoint() → get_memory_entrypoint()
  - isAutoMemPath()        → is_memory_path()
"""

from __future__ import annotations

import os
from pathlib import Path


# Default memory root — ~/.my-agent/memory/
_DEFAULT_MEMORY_DIR = os.path.join(Path.home(), ".my-agent", "memory")


def get_memory_dir() -> str:
    """Return the absolute path to the memory directory.

    Override via MYAGENT_MEMORY_DIR env var; defaults to ~/.my-agent/memory/.
    """
    return os.environ.get("MYAGENT_MEMORY_DIR", _DEFAULT_MEMORY_DIR)


def get_memory_dir_display() -> str:
    """Return the memory directory path normalised for display and bash usage.

    On Windows, os paths use backslashes which break when LLM passes them
    to bash commands.  This returns forward-slash paths that work in both
    Git Bash and display contexts.
    """
    return get_memory_dir().replace("\\", "/")


def get_memory_entrypoint() -> str:
    """Return the absolute path to MEMORY.md (the index file)."""
    return os.path.join(get_memory_dir(), "MEMORY.md")


def ensure_memory_dir() -> None:
    """Create the memory directory if it doesn't exist (idempotent)."""
    os.makedirs(get_memory_dir(), exist_ok=True)


def is_memory_path(path: str) -> bool:
    """Check whether *path* is inside the memory directory."""
    try:
        resolved = os.path.realpath(path)
        mem_dir = os.path.realpath(get_memory_dir())
        return resolved.startswith(mem_dir + os.sep) or resolved == mem_dir
    except (OSError, ValueError):
        return False
