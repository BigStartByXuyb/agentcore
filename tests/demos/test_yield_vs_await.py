"""
验证：yield 不让出给 event loop，await 才让出。

测试 3 个场景：
1. async generator 只有 yield → create_task 的协程完全没机会跑
2. async generator 有 yield + await → create_task 的协程能跑
3. 纯 coroutine 用 await → create_task 的协程能跑
"""

import asyncio
import time


async def background():
    """后台协程，用来观察是否拿到执行机会。"""
    for i in range(5):
        print(f"    [后台协程] tick {i}")
        await asyncio.sleep(0.05)


# ── 场景 1：async generator 只有 yield ──
async def gen_only_yield():
    for i in range(5):
        print(f"    [gen] yield {i}")
        time.sleep(0.1)  # 同步阻塞，模拟耗时
        yield i


# ── 场景 2：async generator 有 yield + await ──
async def gen_yield_with_await():
    for i in range(5):
        print(f"    [gen+await] yield {i}")
        await asyncio.sleep(0.1)  # 让出给 event loop
        yield i


# ── 场景 3：纯 coroutine 用 await ──
async def coro_with_await():
    for i in range(5):
        print(f"    [coroutine] step {i}")
        await asyncio.sleep(0.1)


async def main():
    # ── 场景 1 ──
    print("=" * 50)
    print("场景 1：async generator 只有 yield")
    print("预期：后台协程完全没机会跑")
    print("=" * 50)
    task = asyncio.create_task(background())
    async for val in gen_only_yield():
        pass
    await task
    print()

    # ── 场景 2 ──
    print("=" * 50)
    print("场景 2：async generator 有 yield + await")
    print("预期：后台协程和 generator 交替跑")
    print("=" * 50)
    task = asyncio.create_task(background())
    async for val in gen_yield_with_await():
        pass
    await task
    print()

    # ── 场景 3 ──
    print("=" * 50)
    print("场景 3：纯 coroutine 用 await")
    print("预期：后台协程和 coroutine 交替跑")
    print("=" * 50)
    task = asyncio.create_task(background())
    await coro_with_await()
    await task


if __name__ == "__main__":
    asyncio.run(main())
