"""File watcher system — monitors skills and memory directories for changes.

Uses watchdog (OS-level file watching: ReadDirectoryChangesW on Windows,
inotify on Linux, kqueue on macOS) with debouncing.  Watchdog runs in its
own thread; changes are bridged to asyncio via call_soon_threadsafe.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
log = logging.getLogger(__name__)

SKILL_DEBOUNCE = 0.5   # seconds
MEMORY_DEBOUNCE = 0.3

SKILL_EXTENSIONS = {".md"}
MEMORY_EXTENSIONS = {".md", ".txt", ".text"}


@dataclass
class ChangeFlags:
    """Async-safe flags set by the watcher thread, checked by the REPL."""
    skills_changed: asyncio.Event = field(default_factory=asyncio.Event)
    memory_changed: asyncio.Event = field(default_factory=asyncio.Event)


class _DebouncedNotifier:
    """Coalesces rapid filesystem events into a single notification."""

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 flag: asyncio.Event, delay: float) -> None:
        self._loop = loop
        self._flag = flag
        self._delay = delay
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def trigger(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        self._loop.call_soon_threadsafe(self._flag.set)


def _get_skill_dirs() -> list[str]:
    """Return skill directories (guaranteed to exist)."""
    from src.config import get_skill_dirs
    return get_skill_dirs()


def _get_memory_dir() -> str | None:
    from src.memory.paths import get_memory_dir
    d = get_memory_dir()
    return d if os.path.isdir(d) else None


def start_watchers(loop: asyncio.AbstractEventLoop) -> ChangeFlags:
    """Start file watchers and return change flags.

    If watchdog is not installed, returns inert flags (never set).
    """
    flags = ChangeFlags()

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileSystemEvent
    except ImportError:
        log.info("watchdog not installed — file watching disabled")
        return flags

    skill_notifier = _DebouncedNotifier(loop, flags.skills_changed, SKILL_DEBOUNCE)
    memory_notifier = _DebouncedNotifier(loop, flags.memory_changed, MEMORY_DEBOUNCE)

    skill_dirs = _get_skill_dirs()
    memory_dir = _get_memory_dir()

    if not skill_dirs and not memory_dir:
        log.info("No directories to watch")
        return flags

    class _Handler(FileSystemEventHandler):
        def __init__(self, extensions: set[str], notifier: _DebouncedNotifier):
            self._extensions = extensions
            self._notifier = notifier

        def on_any_event(self, event: FileSystemEvent) -> None:
            if event.is_directory:
                self._notifier.trigger()
                return
            src = getattr(event, "src_path", "")
            if src:
                ext = os.path.splitext(src)[1].lower()
                if ext in self._extensions:
                    self._notifier.trigger()

    observer = Observer()
    observer.daemon = True

    skill_handler = _Handler(SKILL_EXTENSIONS, skill_notifier)
    memory_handler = _Handler(MEMORY_EXTENSIONS, memory_notifier)

    for d in skill_dirs:
        observer.schedule(skill_handler, d, recursive=True)
        log.info(f"Watching skills: {d}")

    if memory_dir:
        observer.schedule(memory_handler, memory_dir, recursive=False)
        log.info(f"Watching memory: {memory_dir}")

    try:
        observer.start()
    except OSError as e:
        log.warning(f"Failed to start file watcher: {e}")
        return flags

    return flags
