"""File state cache — LRU cache tracking recently read files.

Provides deduplication for repeated reads and detects external
modifications between turns (getChangedFiles).

Corresponds to Claude Code's utils/fileStateCache.ts +
the getChangedFiles() portion of utils/attachments.ts.
"""

from __future__ import annotations

import difflib
import os
from collections import OrderedDict
from dataclasses import dataclass

from src.core import config
from src.core.types import Attachment

_DIFF_CONTEXT_LINES = 8
_DIFF_SNIPPET_MAX_CHARS = 8192
_DIFF_SNIPPET_MAX_TOKENS = 5000


@dataclass
class FileState:
    """State of a file at the time it was last read/written."""

    content: str
    mtime: float
    offset: int | None = None
    limit: int | None = None

    @property
    def isPartialView(self) -> bool:
        return self.offset is not None or self.limit is not None


class FileStateCache:
    """LRU cache mapping absolute file paths to their last-read state."""

    def __init__(self, max_size: int = 100):
        self._max_size = max_size
        self._cache: OrderedDict[str, FileState] = OrderedDict()

    def _normalize(self, path: str) -> str:
        return os.path.normpath(os.path.abspath(path))

    def get(self, path: str) -> FileState | None:
        key = self._normalize(path)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def set(self, path: str, state: FileState) -> None:
        key = self._normalize(path)
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = state
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def clear(self) -> None:
        self._cache.clear()

    def delete(self, path: str) -> None:
        key = self._normalize(path)
        self._cache.pop(key, None)

    def keys(self) -> list[str]:
        return list(self._cache.keys())

    def items(self) -> list[tuple[str, FileState]]:
        return list(self._cache.items())

    def __len__(self) -> int:
        return len(self._cache)

    def __bool__(self) -> bool:
        return True

    def snapshot(self) -> dict[str, FileState]:
        return dict(self._cache)

    # ------------------------------------------------------------------
    # External change detection
    # ------------------------------------------------------------------

    def get_changed_files(self) -> list[Attachment]:
        """Scan for externally modified files and return diff attachments.

        For each cached file whose disk mtime exceeds the cached timestamp:
          1. Read the new content from disk
          2. Compute a diff snippet (cached content vs new disk content)
          3. Update the cache entry (so the change is reported only once)
          4. Build an Attachment with the diff snippet

        Corresponds to Claude Code's getChangedFiles() in attachments.ts.
        """
        if not self._cache:
            return []

        attachments: list[Attachment] = []

        for path, state in list(self._cache.items()):
            if state.offset is not None or state.limit is not None:
                continue

            try:
                disk_mtime = os.path.getmtime(path)
            except OSError:
                self._cache.pop(path, None)
                continue

            if disk_mtime <= state.mtime:
                continue

            try:
                from src.utils.file_encoding import read_file_streaming
                lines, _, _ = read_file_streaming(path)
                new_content = "\n".join(lines)
            except Exception:
                self.delete(path)
                text = (
                    f"<system-reminder>\n"
                    f"Note: {path} was modified, either by the user or by a linter. "
                    f"Failed to read the file for diff. "
                    f"Read the file again to see the current content.\n"
                    f"</system-reminder>"
                )
                attachments.append(Attachment(type="system_reminder", content=text))
                continue

            snippet = _compute_diff_snippet(state.content, new_content)

            self.set(path, FileState(
                content=new_content,
                mtime=disk_mtime,
                offset=None,
                limit=None,
            ))

            if not snippet:
                continue

            tokens = config.estimate_tokens(snippet)
            if tokens > _DIFF_SNIPPET_MAX_TOKENS:
                snippet = _truncate_snippet(snippet)

            text = (
                f"<system-reminder>\n"
                f"Note: {path} was modified, either by the user or by a linter. "
                f"This change was intentional, so make sure to take it into account "
                f"as you proceed (ie. don't revert it unless the user asks you to). "
                f"Here are the relevant changes (shown with line numbers):\n"
                f"{snippet}\n"
                f"</system-reminder>"
            )
            attachments.append(Attachment(type="system_reminder", content=text))

        return attachments


# ----------------------------------------------------------------------
# Module-private helpers
# ----------------------------------------------------------------------



def _compute_diff_snippet(old_content: str, new_content: str) -> str:
    """Compute a diff snippet showing changed regions with line numbers.

    Returns the new content around changed hunks (with context lines),
    prefixed with line numbers.  Returns '' if content is identical.
    """
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    opcodes = matcher.get_opcodes()

    if not any(tag != "equal" for tag, *_ in opcodes):
        return ""

    changed: set[int] = set()
    for tag, _, _, j1, j2 in opcodes:
        if tag != "equal":
            for j in range(j1, j2):
                changed.add(j)

    include: set[int] = set()
    for j in changed:
        lo = max(0, j - _DIFF_CONTEXT_LINES)
        hi = min(len(new_lines), j + _DIFF_CONTEXT_LINES + 1)
        for ctx in range(lo, hi):
            include.add(ctx)

    hunks: list[str] = []
    current_hunk: list[str] = []
    prev_j = -2

    for j in sorted(include):
        if j != prev_j + 1 and current_hunk:
            hunks.append("".join(current_hunk))
            current_hunk = []
        line = new_lines[j].rstrip("\r\n")
        current_hunk.append(f"{j + 1}\t{line}\n")
        prev_j = j

    if current_hunk:
        hunks.append("".join(current_hunk))

    full = "\n...\n".join(hunks)

    if len(full) <= _DIFF_SNIPPET_MAX_CHARS:
        return full

    cutoff = full.rfind("\n", 0, _DIFF_SNIPPET_MAX_CHARS)
    if cutoff <= 0:
        cutoff = _DIFF_SNIPPET_MAX_CHARS
    remaining = full[cutoff:].count("\n") + 1
    return f"{full[:cutoff]}\n\n... [{remaining} lines truncated] ..."


def _truncate_snippet(snippet: str) -> str:
    """Truncate a snippet to fit within the token budget."""
    lines = snippet.split("\n")
    kept: list[str] = []
    used = 0
    for line in lines:
        lt = config.estimate_tokens(line)
        if used + lt > _DIFF_SNIPPET_MAX_TOKENS:
            break
        kept.append(line)
        used += lt
    remaining = len(lines) - len(kept)
    return "\n".join(kept) + f"\n\n... [{remaining} lines truncated] ..."
