"""File watcher system — monitors skills and memory directories for changes.

Uses watchdog (OS-level file watching: ReadDirectoryChangesW on Windows,
inotify on Linux, kqueue on macOS) with debouncing.  Watchdog runs in its
own thread; changes are bridged to asyncio via call_soon_threadsafe.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable

log = logging.getLogger(__name__)

SKILL_DEBOUNCE = 0.5   # seconds
MEMORY_DEBOUNCE = 0.3
MCP_DEBOUNCE = 1.0     # MCP connections are expensive — longer debounce
PERMISSION_DEBOUNCE = 0.5

SKILL_EXTENSIONS = {".md"}
MEMORY_EXTENSIONS = {".md", ".txt", ".text"}


# class _DebouncedNotifierThreaded:
#     """Thread-based debouncer — creates/destroys a threading.Timer per trigger.
#
#     def __init__(self, loop: asyncio.AbstractEventLoop,
#                  callback: Callable[[], None], delay: float) -> None:
#         self._loop = loop
#         self._callback = callback
#         self._delay = delay
#         self._timer: threading.Timer | None = None
#         self._lock = threading.Lock()
#
#     def trigger(self) -> None:
#         with self._lock:
#             if self._timer is not None:
#                 self._timer.cancel()
#             self._timer = threading.Timer(self._delay, self._fire)
#             self._timer.daemon = True
#             self._timer.start()
#
#     def _fire(self) -> None:
#         self._loop.call_soon_threadsafe(self._callback)


class _DebouncedNotifier:
    """Asyncio-based debouncer — uses event loop timer instead of OS threads.

    Watchdog emitter threads call trigger(), which crosses into the asyncio
    event loop via call_soon_threadsafe.  A version counter ensures that
    stale _reschedule calls (queued but outdated) skip work immediately.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 callback: Callable[[], None], delay: float) -> None:
        self._loop = loop
        self._callback = callback
        self._delay = delay
        self._handle: asyncio.TimerHandle | None = None
        self._version = 0

    def trigger(self) -> None:
        self._version += 1
        ver = self._version
        self._loop.call_soon_threadsafe(self._reschedule, ver)

    def _reschedule(self, ver: int) -> None:
        if ver != self._version:
            return
        if self._handle is not None:
            self._handle.cancel()
        self._handle = self._loop.call_later(self._delay, self._callback)


def _get_skill_dirs() -> list[str]:
    """Return skill directories (guaranteed to exist)."""
    from src.core.config import get_skill_dirs
    return get_skill_dirs()


def _get_agent_dirs() -> list[str]:
    """Return agent directories (guaranteed to exist)."""
    from src.core.config import get_agent_dirs
    return get_agent_dirs()


def _get_memory_dir() -> str | None:
    from src.memory.paths import get_memory_dir
    d = get_memory_dir()
    return d if os.path.isdir(d) else None


def _reload_skills() -> None:
    from src.skills import get_skills, reset_sent_skills
    get_skills(force_reload=True)
    reset_sent_skills()
    log.info("[watcher] Skills reloaded.")


def _reload_agents() -> None:
    from src.agents import get_agents, reset_sent_agents
    get_agents(force_reload=True)
    reset_sent_agents()
    log.info("[watcher] Agents reloaded.")
    print("[watcher] Agents reloaded.")


def _reload_mcp() -> None:
    from src.mcp_tool import reload_mcp_servers
    asyncio.ensure_future(reload_mcp_servers())


def _reload_permissions() -> None:
    import src.agent_loop as _al
    engine = _al._permission_engine
    if engine is not None:
        engine.reload()
        log.info("[watcher] Permissions reloaded.")


def _get_permission_config_paths() -> list[str]:
    """Return permission config file paths (that exist on disk)."""
    from src.core.config import get_permission_config_paths
    return [p for p in get_permission_config_paths() if os.path.isfile(p)]


def _reload_memory() -> None:
    log.info("[watcher] Memory files changed — will pick up on next recall.")


def start_watchers(loop: asyncio.AbstractEventLoop) -> None:
    """Start file watchers with direct reload callbacks.

    If watchdog is not installed, does nothing.
    """
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileSystemEvent
    except ImportError:
        log.info("watchdog not installed — file watching disabled")
        return

    skill_notifier = _DebouncedNotifier(loop, _reload_skills, SKILL_DEBOUNCE)
    agent_notifier = _DebouncedNotifier(loop, _reload_agents, SKILL_DEBOUNCE)
    memory_notifier = _DebouncedNotifier(loop, _reload_memory, MEMORY_DEBOUNCE)
    mcp_notifier = _DebouncedNotifier(loop, _reload_mcp, MCP_DEBOUNCE)

    skill_dirs = _get_skill_dirs()
    agent_dirs = _get_agent_dirs()
    memory_dir = _get_memory_dir()

    from src.mcp_tool.config import get_mcp_config_paths
    mcp_paths = get_mcp_config_paths()

    if not skill_dirs and not agent_dirs and not memory_dir and not mcp_paths:
        log.info("No directories to watch")
        return

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
    agent_handler = _Handler(SKILL_EXTENSIONS, agent_notifier)
    memory_handler = _Handler(MEMORY_EXTENSIONS, memory_notifier)

    for d in skill_dirs:
        observer.schedule(skill_handler, d, recursive=True)
        log.info(f"Watching skills: {d}")

    for d in agent_dirs:
        observer.schedule(agent_handler, d, recursive=True)
        log.info(f"Watching agents: {d}")

    if memory_dir:
        observer.schedule(memory_handler, memory_dir, recursive=False)
        log.info(f"Watching memory: {memory_dir}")

    # MCP config file watcher — filters by exact filename, not extension
    class _McpConfigHandler(FileSystemEventHandler):
        def __init__(self, watch_filenames: set[str],
                     notifier: _DebouncedNotifier):
            self._watch_filenames = watch_filenames
            self._notifier = notifier

        def on_any_event(self, event: FileSystemEvent) -> None:
            if event.is_directory:
                return
            src = getattr(event, "src_path", "")
            if src and os.path.basename(src) in self._watch_filenames:
                self._notifier.trigger()

    mcp_handler = _McpConfigHandler(
        {p.name for p, _scope in mcp_paths}, mcp_notifier)
    for p, _scope in mcp_paths:
        parent = str(p.parent)
        if os.path.isdir(parent):
            observer.schedule(mcp_handler, parent, recursive=False)
            log.info(f"Watching MCP config: {p}")

    # Permission config file watcher
    permission_notifier = _DebouncedNotifier(
        loop, _reload_permissions, PERMISSION_DEBOUNCE)
    permission_paths = _get_permission_config_paths()
    if permission_paths:
        perm_handler = _McpConfigHandler(
            {os.path.basename(p) for p in permission_paths},
            permission_notifier,
        )
        watched_perm_dirs: set[str] = set()
        for p in permission_paths:
            parent = os.path.dirname(p)
            if parent not in watched_perm_dirs and os.path.isdir(parent):
                observer.schedule(perm_handler, parent, recursive=False)
                watched_perm_dirs.add(parent)
                log.info(f"Watching permissions: {p}")

    try:
        observer.start()
    except OSError as e:
        log.warning(f"Failed to start file watcher: {e}")
