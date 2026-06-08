"""Scan memory files and build manifests for recall/extraction prompts."""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone

from src.core.types import MemoryHeader, MemoryType
from src.frontmatter import parse_frontmatter
logger = logging.getLogger(__name__)

# Only scan these extensions
_MEMORY_EXTENSIONS = {".md", ".txt", ".text"}

# Limits
_MAX_FRONTMATTER_LINES = 30
_MAX_FILES = 200


def scan_memory_files(memory_dir: str) -> list[MemoryHeader]:
    """Scan *memory_dir* for memory files, parse frontmatter headers.

    Returns MemoryHeader list sorted by mtime descending, capped at
    MAX_FILES.  Excludes MEMORY.md (the index file — already loaded
    separately in the system prompt).
    """
    if not os.path.isdir(memory_dir):
        return []

    headers: list[MemoryHeader] = []

    for entry in os.scandir(memory_dir):
        if not entry.is_file():
            continue
        _, ext = os.path.splitext(entry.name)
        if ext.lower() not in _MEMORY_EXTENSIONS:
            continue
        if entry.name.upper() == "MEMORY.MD":
            continue

        try:
            stat = entry.stat()
            # Read only the first _MAX_FRONTMATTER_LINES for parsing
            with open(entry.path, "r", encoding="utf-8", errors="replace") as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= _MAX_FRONTMATTER_LINES:
                        break
                    lines.append(line)
                raw_head = "".join(lines)

            fm, _ = parse_frontmatter(raw_head)

            # Validate type field
            mem_type: MemoryType | None = None
            raw_type = fm.get("type")
            if raw_type in ("user", "feedback", "project", "reference"):
                mem_type = raw_type

            headers.append(MemoryHeader(
                filename=entry.name,
                file_path=entry.path,
                mtime=stat.st_mtime,
                name=fm.get("name"),
                description=fm.get("description"),
                type=mem_type,
            ))
        except OSError as e:
            logger.debug("Skipping memory file %s: %s", entry.path, e)

    # Sort newest-first, cap at limit
    headers.sort(key=lambda h: h.mtime, reverse=True)
    return headers[:_MAX_FILES]


def format_memory_manifest(headers: list[MemoryHeader]) -> str:
    """Format scanned headers into a one-line-per-file manifest.

    Used by both recall (side query prompt) and extraction prompts.
    Format: `[type] filename (YYYY-MM-DD): description`
    """
    lines: list[str] = []
    for h in headers:
        type_tag = f"[{h.type}]" if h.type else "[unknown]"
        date_str = datetime.fromtimestamp(h.mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        desc = h.description or h.name or "(no description)"
        lines.append(f"{type_tag} {h.filename} ({date_str}): {desc}")
    return "\n".join(lines)
