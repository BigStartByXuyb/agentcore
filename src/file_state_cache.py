"""File state cache — LRU cache tracking recently read files for deduplication."""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class FileState:
    """State of a file at the time it was last read/written."""

    content: str
    mtime: float
    offset: int | None = None
    limit: int | None = None
    isPartialView: bool = False


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

    def snapshot(self) -> dict[str, FileState]:
        return dict(self._cache)
