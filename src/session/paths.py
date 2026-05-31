"""Session directory path resolution.

Follows the same pattern as memory/paths.py.
Storage location: ~/.my-agent/sessions/{sanitized_cwd}/{session_id}.jsonl

Sessions are grouped by project directory so --resume only shows
sessions from the current project.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


_DEFAULT_SESSIONS_BASE = os.path.join(Path.home(), ".my-agent", "sessions")
_MAX_SANITIZED_LENGTH = 200


def _sanitize_path(path: str) -> str:
    """Sanitize a directory path into a safe folder name.

    Replaces non-alphanumeric characters with hyphens.
    Truncates + appends hash if too long.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", path)
    if len(sanitized) > _MAX_SANITIZED_LENGTH:
        import hashlib
        h = hashlib.md5(path.encode()).hexdigest()[:8]
        sanitized = sanitized[:_MAX_SANITIZED_LENGTH] + "-" + h
    return sanitized


def get_sessions_dir() -> str:
    """Return the absolute path to the sessions directory for the current project.

    Override via MYAGENT_SESSIONS_DIR env var (bypasses project grouping).
    Default: ~/.my-agent/sessions/{sanitized_cwd}/
    """
    override = os.environ.get("MYAGENT_SESSIONS_DIR")
    if override:
        return override
    project_dir = _sanitize_path(os.getcwd())
    return os.path.join(_DEFAULT_SESSIONS_BASE, project_dir)


def ensure_sessions_dir() -> None:
    """Create the sessions directory if it doesn't exist (idempotent)."""
    os.makedirs(get_sessions_dir(), exist_ok=True)


def get_session_path(session_id: str) -> str:
    """Return the absolute path to a session's JSONL file."""
    return os.path.join(get_sessions_dir(), f"{session_id}.jsonl")
