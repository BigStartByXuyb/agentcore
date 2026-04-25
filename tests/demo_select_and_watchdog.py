"""Demo: select IO multiplexing + watchdog observer.

Run: python tests/demo_select_and_watchdog.py
"""

import select
import socket
import threading
import time
import tempfile
import os

# ═══════════════════════════════════════════════════════════════════
# Demo 1: select — 一个线程监听多个 fd，知道哪个触发了
# ═══════════════════════════════════════════════════════════════════

def demo_select():
    print("=" * 60)
    print("Demo 1: select — 多路复用，一个线程监听多个 socket")
    print("=" * 60)

    # Windows 的 select 只支持 socket，所以用 socket pair 模拟
    # 创建 3 个 "通道"，每个通道有 sender 和 receiver
    channels = {}
    for name in ["skills", "agents", "mcp"]:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        sender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sender.connect(("127.0.0.1", port))
        receiver, _ = server.accept()
        server.close()

        channels[name] = {"sender": sender, "receiver": receiver}

    # fd → name 的映射表（类似 observer 的 directory → handler 映射）
    fd_to_name = {}
    watch_list = []
    for name, ch in channels.items():
        r = ch["receiver"]
        fd_to_name[r.fileno()] = name
        watch_list.append(r)

    print(f"\n注册的映射表: { {r.fileno(): name for r, name in zip(watch_list, channels.keys())} }")
    print("开始 select 监听...\n")

    # 模拟：不同时间往不同通道写数据
    def simulate_events():
        time.sleep(0.5)
        print("  [模拟] 往 'mcp' 通道写入数据...")
        channels["mcp"]["sender"].send(b"mcp.json changed")

        time.sleep(0.5)
        print("  [模拟] 往 'skills' 通道写入数据...")
        channels["skills"]["sender"].send(b"commit.md changed")

        time.sleep(0.5)
        print("  [模拟] 同时往 'agents' 和 'mcp' 写入数据...")
        channels["agents"]["sender"].send(b"explore.md changed")
        channels["mcp"]["sender"].send(b".mcp.json changed")

        time.sleep(0.5)
        # 关闭所有 sender 让 select 循环结束
        for ch in channels.values():
            ch["sender"].close()

    t = threading.Thread(target=simulate_events, daemon=True)
    t.start()

    # select 主循环 — 类似 observer 的内部循环
    rounds = 0
    while watch_list and rounds < 10:
        # select 阻塞，直到至少一个 fd 有数据
        readable, _, _ = select.select(watch_list, [], [], 3.0)

        if not readable:
            break

        # ★ 关键：select 返回的是「哪些 fd 就绪了」
        for sock in readable:
            fd = sock.fileno()
            name = fd_to_name[fd]  # 通过映射表找到是谁
            data = sock.recv(1024)
            if not data:
                watch_list.remove(sock)
                sock.close()
                continue
            print(f"  [select] fd={fd} 触发 → 映射到 '{name}' → 数据: {data.decode()}")

        rounds += 1

    # 清理
    for ch in channels.values():
        for s in ch.values():
            try:
                s.close()
            except:
                pass

    t.join(timeout=2)
    print("\nselect demo done\n")


# ═══════════════════════════════════════════════════════════════════
# Demo 2: watchdog observer — 实际文件监听，看事件分发
# ═══════════════════════════════════════════════════════════════════

def demo_watchdog():
    print("=" * 60)
    print("Demo 2: watchdog observer — 文件监听 + handler 分发")
    print("=" * 60)

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("watchdog 未安装，跳过此 demo")
        return

    # 创建两个临时目录，模拟 skills 和 mcp 配置目录
    tmpdir = tempfile.mkdtemp()
    skills_dir = os.path.join(tmpdir, "skills")
    mcp_dir = os.path.join(tmpdir, "mcp_config")
    os.makedirs(skills_dir)
    os.makedirs(mcp_dir)

    print(f"\n监听目录:")
    print(f"  skills: {skills_dir}")
    print(f"  mcp:    {mcp_dir}")

    # 两个不同的 handler，各自只收到自己目录的事件
    class SkillHandler(FileSystemEventHandler):
        def on_any_event(self, event):
            print(f"  [SkillHandler] {event.event_type}: {os.path.basename(event.src_path)}")

    class McpHandler(FileSystemEventHandler):
        def on_any_event(self, event):
            basename = os.path.basename(event.src_path)
            if basename == "mcp.json":
                print(f"  [McpHandler]   {event.event_type}: {basename} HIT!")
            else:
                print(f"  [McpHandler]   {event.event_type}: {basename} SKIP")

    observer = Observer()  # 只有 1 个线程
    observer.schedule(SkillHandler(), skills_dir)
    observer.schedule(McpHandler(), mcp_dir)
    observer.start()

    print("\n开始文件操作...\n")

    # 操作 1: 在 skills 目录创建文件 → 只有 SkillHandler 收到
    time.sleep(0.3)
    print("--- 创建 skills/commit.md ---")
    with open(os.path.join(skills_dir, "commit.md"), "w") as f:
        f.write("skill content")
    time.sleep(0.5)

    # 操作 2: 在 mcp 目录创建 mcp.json → 只有 McpHandler 收到
    print("\n--- 创建 mcp_config/mcp.json ---")
    with open(os.path.join(mcp_dir, "mcp.json"), "w") as f:
        f.write('{"mcpServers": {}}')
    time.sleep(0.5)

    # 操作 3: 在 mcp 目录创建无关文件 → McpHandler 收到但忽略
    print("\n--- 创建 mcp_config/other.json ---")
    with open(os.path.join(mcp_dir, "other.json"), "w") as f:
        f.write('{"unrelated": true}')
    time.sleep(0.5)

    # 操作 4: 同时操作两个目录
    print("\n--- 同时操作两个目录 ---")
    with open(os.path.join(skills_dir, "review.md"), "w") as f:
        f.write("review skill")
    with open(os.path.join(mcp_dir, "mcp.json"), "w") as f:
        f.write('{"mcpServers": {"github": {}}}')
    time.sleep(0.5)

    observer.stop()
    observer.join()

    # 清理临时目录
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    print("\nwatchdog demo done\n")


if __name__ == "__main__":
    demo_select()
    print()
    demo_watchdog()
