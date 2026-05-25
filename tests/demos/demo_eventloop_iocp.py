"""Demo: asyncio event loop IOCP blocking mechanism.

Shows the real flow:
  coroutines all await -> nothing to do -> thread blocks at IOCP
  -> IO completes -> OS wakes thread -> coroutine resumes

Run: python tests/demo_eventloop_iocp.py
"""

import asyncio
import socket
import threading
import time


# =====================================================================
# Part 1: Manual simulation — see the blocking point yourself
# =====================================================================

def demo_manual_iocp_simulation():
    print("=" * 60)
    print("Part 1: IOCP event loop simulation (manual)")
    print("=" * 60)
    print()

    # Simulate the event loop's core data structures
    _ready = []          # callbacks ready to run (like loop._ready)
    _cache = {}          # fd -> callback mapping (like proactor._cache)
    _selector_fds = []   # fds to monitor

    # Create two sockets to simulate two coroutines waiting on IO
    pairs = {}
    for name in ["http_request", "db_query"]:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        sender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sender.connect(("127.0.0.1", port))
        receiver, _ = srv.accept()
        srv.close()
        pairs[name] = {"sender": sender, "receiver": receiver}
        _selector_fds.append(receiver)
        # Register callback in cache (simulates Future + coroutine.__step)
        _cache[receiver.fileno()] = name

    print("  Registered IO waiters:")
    for fd_obj in _selector_fds:
        print(f"    fd={fd_obj.fileno()} -> coroutine '{_cache[fd_obj.fileno()]}'")

    # Simulate external IO arriving at different times
    def simulate_io():
        time.sleep(1)
        print("\n  [external] db_query response arrived!")
        pairs["db_query"]["sender"].send(b'{"rows": 42}')
        time.sleep(1)
        print("  [external] http_request response arrived!")
        pairs["http_request"]["sender"].send(b'<html>OK</html>')

    t = threading.Thread(target=simulate_io, daemon=True)
    t.start()

    # ---- The actual event loop simulation ----
    print("\n  --- Event loop starts ---\n")

    remaining = set(_cache.keys())
    while remaining:
        # Step 1: Execute all ready callbacks
        if _ready:
            print("  [loop] Step 1: Running ready callbacks...")
            while _ready:
                name, data = _ready.pop(0)
                print(f"    -> coroutine '{name}' resumes with data: {data}")
                remaining.discard(name)  # coroutine finished
        else:
            print("  [loop] Step 1: No ready callbacks. All coroutines are awaiting.")

        if not remaining:
            break

        if not _selector_fds:
            break

        # Step 2: Block on IO — this is GetQueuedCompletionStatus / select
        print("  [loop] Step 2: Thread BLOCKS at select/IOCP... (waiting for IO)")
        import select as _select
        readable, _, _ = _select.select(_selector_fds, [], [])
        # ^^^ Thread is suspended here until OS wakes it ^^^

        # Step 3: Woke up! OS tells us which fd(s) have data
        print("  [loop] Step 3: WOKE UP! Processing completed IO:")
        for sock in readable:
            fd = sock.fileno()
            name = _cache[fd]
            data = sock.recv(1024).decode()
            print(f"    -> fd={fd} ready, coroutine '{name}' -> add to _ready queue")
            _ready.append((name, data))
            _selector_fds.remove(sock)

        print()

    print("  --- Event loop: all coroutines done ---\n")

    # Cleanup
    for p in pairs.values():
        p["sender"].close()
        p["receiver"].close()
    t.join(timeout=3)


# =====================================================================
# Part 2: Real asyncio — trace the actual blocking with monkey-patch
# =====================================================================

def demo_real_asyncio_trace():
    print("=" * 60)
    print("Part 2: Real asyncio event loop — trace the blocking point")
    print("=" * 60)
    print()

    loop = asyncio.new_event_loop()

    # Monkey-patch _run_once to show when it blocks and resumes
    original_run_once = loop._run_once

    run_once_count = [0]

    def traced_run_once():
        run_once_count[0] += 1
        n = run_once_count[0]
        ready_count = len(loop._ready)
        scheduled_count = len(loop._scheduled)

        if ready_count > 0:
            print(f"  [_run_once #{n}] {ready_count} ready callbacks, "
                  f"{scheduled_count} scheduled timers")
        else:
            print(f"  [_run_once #{n}] nothing ready -> will BLOCK at IOCP "
                  f"(scheduled timers: {scheduled_count})")

        t0 = time.perf_counter()
        original_run_once()
        elapsed = time.perf_counter() - t0

        if elapsed > 0.05:
            print(f"  [_run_once #{n}] woke up after {elapsed:.3f}s (was blocked at IOCP)")

    loop._run_once = traced_run_once

    async def fake_io_task(name, delay):
        print(f"  [coroutine '{name}'] starts, will await {delay}s...")
        await asyncio.sleep(delay)  # internally: register timer -> suspend -> IOCP blocks
        print(f"  [coroutine '{name}'] resumed! done.")
        return f"{name}_result"

    async def main():
        print("  [main] creating 2 coroutines...\n")

        task1 = asyncio.create_task(fake_io_task("fast_task", 0.5))
        task2 = asyncio.create_task(fake_io_task("slow_task", 1.5))

        results = await asyncio.gather(task1, task2)
        print(f"\n  [main] all done: {results}")

    print("  Note: asyncio.sleep uses a timer, not real IO.")
    print("  But the mechanism is the same: nothing ready -> block at IOCP -> wake on timer\n")

    loop.run_until_complete(main())
    loop.close()
    print()


# =====================================================================
# Part 3: Real asyncio with real socket IO
# =====================================================================

def demo_real_socket_io():
    print("=" * 60)
    print("Part 3: Real asyncio with socket IO — full IOCP flow")
    print("=" * 60)
    print()

    async def run():
        # Start a simple TCP server
        clients_done = asyncio.Event()

        async def handle_client(reader, writer):
            data = await reader.read(1024)
            print(f"  [server] received: {data.decode()}")
            writer.write(b"pong")
            await writer.drain()
            writer.close()
            clients_done.set()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        print(f"  [server] listening on port {port}")

        # Coroutine 1: connect and send data (real socket IO)
        async def client_task():
            print("  [client] connecting...")
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            # ^^^ await here -> coroutine suspends -> event loop blocks at IOCP
            #     -> OS completes TCP handshake -> IOCP returns -> coroutine resumes

            print("  [client] connected! sending 'ping'...")
            writer.write(b"ping")
            await writer.drain()
            # ^^^ await here -> event loop blocks at IOCP -> socket write completes

            data = await reader.read(1024)
            # ^^^ await here -> event loop blocks at IOCP -> data arrives from server
            print(f"  [client] received: {data.decode()}")

            writer.close()

        # Coroutine 2: runs concurrently, proves event loop serves both
        async def timer_task():
            print("  [timer] starting concurrent timer...")
            await asyncio.sleep(0.1)
            print("  [timer] tick! (event loop served me while client was doing IO)")

        print("  Starting client + timer concurrently...\n")
        await asyncio.gather(client_task(), timer_task())
        await clients_done.wait()

        server.close()
        await server.wait_closed()
        print("\n  All coroutines done. Every 'await' was a potential IOCP block point.")

    asyncio.run(run())
    print()


if __name__ == "__main__":
    demo_manual_iocp_simulation()
    print()
    demo_real_asyncio_trace()
    print()
    demo_real_socket_io()
