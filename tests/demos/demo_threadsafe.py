"""
演示：跨线程设置 asyncio.Future 的正确 vs 错误方式

错误方式：直接 future.set_result() → event loop 可能卡在 select 上不知道
正确方式：call_soon_threadsafe() → 通过 self-pipe 唤醒 event loop
"""

import asyncio
import threading
import time


async def wait_for_future(fut, label):
    print(f"[{label}] coroutine starts await future...")
    start = time.time()
    result = await fut
    elapsed = time.time() - start
    print(f"[{label}] coroutine woke up! took {elapsed:.3f}s, result: {result}")


def test_wrong_way():
    print("\n===== WRONG: direct set_result =====")
    print("(if no response in 5s, event loop is stuck in select)")
    loop = asyncio.new_event_loop()
    fut = loop.create_future()

    def other_thread():
        time.sleep(1)
        print(f"[other thread] direct fut.set_result('hello') -- no self-pipe wakeup!")
        fut.set_result("hello")
        time.sleep(5)
        print(f"[other thread] 5s passed, event loop still stuck, force stop")
        loop.call_soon_threadsafe(loop.stop)

    t = threading.Thread(target=other_thread)
    t.start()
    try:
        loop.run_until_complete(asyncio.wait_for(fut, timeout=6))
    except asyncio.TimeoutError:
        print("[WRONG] timeout! fut has result but event loop didn't know")
    t.join()
    loop.close()


def test_right_way():
    print("\n===== RIGHT: call_soon_threadsafe =====")
    loop = asyncio.new_event_loop()
    fut = loop.create_future()

    def other_thread():
        time.sleep(1)
        print(f"[other thread] call_soon_threadsafe to set result")
        loop.call_soon_threadsafe(fut.set_result, "hello")
        # 内部：
        # 1. 把 fut.set_result("hello") 塞进回调队列
        # 2. 往 self-pipe 写一个字节
        # 3. select 检测到 self-pipe 可读 → 立即返回
        # 4. event loop 执行回调 → future 有结果了 → 协程被唤醒

    t = threading.Thread(target=other_thread)
    t.start()
    loop.run_until_complete(wait_for_future(fut, "正确"))
    t.join()
    loop.close()


if __name__ == "__main__":
    test_right_way()
    test_wrong_way()
