"""验证 call_soon_threadsafe 的行为：
1. callback 在哪个线程执行？
2. 多个 callback 是一口气执行还是穿插协程？
3. callback 会不会阻塞协程？
"""

import asyncio
import threading
import time


async def background_coroutine():
    """后台协程，每 0.1 秒打印一次，观察是否被 callback 阻塞。"""
    for i in range(10):
        print(f"  [协程] tick {i}  线程={threading.current_thread().name}")
        await asyncio.sleep(0.1)


def heavy_callback(index):
    """模拟耗时 callback，观察是否阻塞协程。"""
    print(f"  [callback-{index}] 开始  线程={threading.current_thread().name}")
    time.sleep(0.3)  # 同步阻塞 0.3 秒
    print(f"  [callback-{index}] 结束  线程={threading.current_thread().name}")


def light_callback(index):
    """轻量 callback。"""
    print(f"  [callback-{index}] 执行  线程={threading.current_thread().name}")


def inject_from_thread(loop):
    """从另一个线程往事件循环队列里塞 callback。"""
    time.sleep(0.25)  # 等一下让协程先跑几轮
    print(f"\n--- 从线程 {threading.current_thread().name} 投递 3 个轻量 callback ---")
    for i in range(3):
        loop.call_soon_threadsafe(light_callback, i)

    time.sleep(0.5)
    print(f"\n--- 从线程 {threading.current_thread().name} 投递 3 个耗时 callback ---")
    for i in range(99):
        loop.call_soon_threadsafe(heavy_callback, i)


async def main():
    print(f"事件循环线程: {threading.current_thread().name}\n")

    # 启动后台协程
    task = asyncio.create_task(background_coroutine())

    # 从另一个线程投递 callback
    loop = asyncio.get_running_loop()
    t = threading.Thread(target=inject_from_thread, args=(loop,), name="Worker线程")
    t.start()

    await task
    t.join()
    print("\n--- 测试结束 ---")


if __name__ == "__main__":
    asyncio.run(main())
