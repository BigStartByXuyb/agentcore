"""Demo: watchdog Observer internal architecture simulation.

Simulates the real watchdog architecture on Windows:
- Each directory gets its own Emitter thread (calls ReadDirectoryChangesW)
- One shared EventQueue bridges emitters to dispatcher
- One Dispatcher thread (Observer itself) consumes queue and routes to handlers

Real watchdog flow:
  Emitter thread (per dir) → ReadDirectoryChangesW (blocks) → queue_event → EventQueue
  Dispatcher thread (1)    → queue.get (blocks) → lookup handler by watch → handler.dispatch

Run: python tests/demo_observer_internals.py
"""

import ctypes
import ctypes.wintypes
import os
import queue
import tempfile
import threading
import time


# =====================================================================
# Part 1: Simulate the real watchdog architecture with plain threads
# =====================================================================

class FakeEvent:
    """Simulates watchdog FileSystemEvent."""
    def __init__(self, event_type, src_path, is_directory=False):
        self.event_type = event_type
        self.src_path = src_path
        self.is_directory = is_directory

    def __repr__(self):
        return f"Event({self.event_type}, {os.path.basename(self.src_path)})"


class EmitterThread(threading.Thread):
    """Simulates watchdog EventEmitter.

    Real watchdog: each emitter has its own thread, calls
    ReadDirectoryChangesW in a loop (blocking), converts
    raw OS events to FileSystemEvents, puts them on the
    shared EventQueue tagged with its watch object.
    """
    def __init__(self, event_queue, watch_path, watch_id):
        super().__init__(daemon=True)
        self._queue = event_queue
        self._path = watch_path
        self._watch_id = watch_id
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        print(f"  [Emitter-{self._watch_id}] thread started, watching: {self._path}")

        # Real code: while True: ReadDirectoryChangesW(handle, ...) → blocks
        # We simulate with polling for simplicity
        known_files = set()
        while not self._stop_event.is_set():
            try:
                current = set(os.listdir(self._path))
            except OSError:
                break

            new_files = current - known_files
            for f in new_files:
                event = FakeEvent("created", os.path.join(self._path, f))
                # Key: event is tagged with watch_id so dispatcher knows
                # which handler to call
                self._queue.put((event, self._watch_id))
                print(f"  [Emitter-{self._watch_id}] detected: {f} -> put to queue")

            known_files = current
            self._stop_event.wait(0.2)  # polling interval

        print(f"  [Emitter-{self._watch_id}] thread stopped")


class ObserverSimulation:
    """Simulates watchdog BaseObserver.

    Architecture:
    - self._emitters: dict of watch_id -> EmitterThread (one per directory)
    - self._handlers: dict of watch_id -> handler callable
    - self._event_queue: shared queue, all emitters write here
    - self._dispatcher: one thread that reads from queue and calls handlers
    """
    def __init__(self):
        self._event_queue = queue.Queue()
        self._handlers = {}       # watch_id -> handler
        self._emitters = {}       # watch_id -> EmitterThread
        self._dispatcher = None
        self._running = False

    def schedule(self, handler, path, watch_id):
        """Register a directory to watch with a handler.

        Real watchdog creates an Emitter per directory.
        """
        emitter = EmitterThread(self._event_queue, path, watch_id)
        self._emitters[watch_id] = emitter
        self._handlers[watch_id] = handler
        print(f"  [Observer] scheduled: watch_id={watch_id}, path={path}")

    def start(self):
        self._running = True
        # Start all emitter threads
        for emitter in self._emitters.values():
            emitter.start()
        # Start dispatcher thread (Observer itself is this thread in real watchdog)
        self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatcher.start()
        print(f"  [Observer] started: {len(self._emitters)} emitter(s) + 1 dispatcher")

    def _dispatch_loop(self):
        """The dispatcher: reads from shared queue, routes to correct handler.

        Real watchdog code (api.py line 378-392):
            def dispatch_events(self, event_queue):
                entry = event_queue.get(block=True)
                event, watch = entry
                for handler in self._handlers[watch]:
                    handler.dispatch(event)
        """
        while self._running:
            try:
                event, watch_id = self._event_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            handler = self._handlers.get(watch_id)
            if handler:
                print(f"  [Dispatcher] queue -> watch_id={watch_id} -> calling {handler.__name__}")
                handler(event)

    def stop(self):
        self._running = False
        for emitter in self._emitters.values():
            emitter.stop()


def demo_observer_simulation():
    print("=" * 60)
    print("Part 1: Observer architecture simulation")
    print("=" * 60)
    print()
    print("Architecture:")
    print("  Emitter-skills (thread)  --+")
    print("  Emitter-mcp    (thread)  --+--> EventQueue --> Dispatcher (thread)")
    print("                                                    |")
    print("                                   watch_id -> handler mapping")
    print()

    tmpdir = tempfile.mkdtemp()
    skills_dir = os.path.join(tmpdir, "skills")
    mcp_dir = os.path.join(tmpdir, "mcp")
    os.makedirs(skills_dir)
    os.makedirs(mcp_dir)

    def skill_handler(event):
        print(f"    >> skill_handler called: {event}")

    def mcp_handler(event):
        fname = os.path.basename(event.src_path)
        if fname == "mcp.json":
            print(f"    >> mcp_handler called: {event} -> RELOAD MCP!")
        else:
            print(f"    >> mcp_handler called: {event} -> skip")

    obs = ObserverSimulation()
    obs.schedule(skill_handler, skills_dir, watch_id="skills")
    obs.schedule(mcp_handler, mcp_dir, watch_id="mcp")
    obs.start()
    print()

    time.sleep(0.5)

    # Create file in skills dir
    print("--- Create skills/commit.md ---")
    with open(os.path.join(skills_dir, "commit.md"), "w") as f:
        f.write("test")
    time.sleep(0.5)

    # Create file in mcp dir
    print("\n--- Create mcp/mcp.json ---")
    with open(os.path.join(mcp_dir, "mcp.json"), "w") as f:
        f.write("{}")
    time.sleep(0.5)

    # Create unrelated file in mcp dir
    print("\n--- Create mcp/other.txt ---")
    with open(os.path.join(mcp_dir, "other.txt"), "w") as f:
        f.write("unrelated")
    time.sleep(0.5)

    obs.stop()
    time.sleep(0.3)

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("\nPart 1 done\n")


# =====================================================================
# Part 2: Actual Windows ReadDirectoryChangesW call demo
# =====================================================================

def demo_real_win_api():
    print("=" * 60)
    print("Part 2: Real ReadDirectoryChangesW — OS-level file watching")
    print("=" * 60)

    kernel32 = ctypes.WinDLL("kernel32")

    # Constants
    FILE_LIST_DIRECTORY = 1
    FILE_SHARE_READ = 0x01
    FILE_SHARE_WRITE = 0x02
    FILE_SHARE_DELETE = 0x04
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_NOTIFY_CHANGE_FILE_NAME = 0x01
    FILE_NOTIFY_CHANGE_LAST_WRITE = 0x10

    # Create temp dir
    tmpdir = tempfile.mkdtemp()
    print(f"\n  Watching directory: {tmpdir}")

    # Step 1: CreateFileW — get a HANDLE to the directory
    handle = kernel32.CreateFileW(
        tmpdir,
        FILE_LIST_DIRECTORY,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    print(f"  Directory handle: {handle}")

    # Prepare buffer
    buf = ctypes.create_string_buffer(4096)
    bytes_returned = ctypes.wintypes.DWORD()

    # Create a file in a separate thread (because ReadDirectoryChangesW blocks)
    def create_files():
        time.sleep(0.5)
        print("\n  [writer] Creating test1.txt...")
        with open(os.path.join(tmpdir, "test1.txt"), "w") as f:
            f.write("hello")
        time.sleep(0.5)
        print("  [writer] Creating test2.txt...")
        with open(os.path.join(tmpdir, "test2.txt"), "w") as f:
            f.write("world")

    writer = threading.Thread(target=create_files, daemon=True)
    writer.start()

    # Step 2: ReadDirectoryChangesW — blocks until something changes
    for i in range(2):
        print(f"\n  [watcher] Calling ReadDirectoryChangesW (blocks until change)...")

        # This is the actual Windows API call — BLOCKS here
        success = kernel32.ReadDirectoryChangesW(
            handle,
            ctypes.byref(buf),
            len(buf),
            False,                   # not recursive
            FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_LAST_WRITE,
            ctypes.byref(bytes_returned),
            None,                    # not overlapped (synchronous = blocking)
            None,
        )

        if success and bytes_returned.value > 0:
            # Parse the FILE_NOTIFY_INFORMATION structure
            offset = 0
            while True:
                fni = ctypes.cast(ctypes.byref(buf, offset),
                                  ctypes.POINTER(FileNotifyInfo))[0]
                name_ptr = ctypes.addressof(fni) + FileNotifyInfo.FileName.offset
                name = ctypes.string_at(name_ptr, fni.FileNameLength).decode("utf-16")
                action_names = {1: "CREATED", 2: "DELETED", 3: "MODIFIED",
                                4: "RENAMED_OLD", 5: "RENAMED_NEW"}
                action = action_names.get(fni.Action, f"UNKNOWN({fni.Action})")
                print(f"  [watcher] OS returned: action={action}, file={name}")

                if fni.NextEntryOffset == 0:
                    break
                offset += fni.NextEntryOffset

    # Cleanup
    kernel32.CloseHandle(handle)
    writer.join(timeout=3)
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("\nPart 2 done\n")


# ctypes structure for parsing ReadDirectoryChangesW results
class FileNotifyInfo(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", ctypes.wintypes.DWORD),
        ("Action", ctypes.wintypes.DWORD),
        ("FileNameLength", ctypes.wintypes.DWORD),
        ("FileName", ctypes.c_char * 1),
    ]


if __name__ == "__main__":
    demo_observer_simulation()
    print()
    demo_real_win_api()
