"""
模拟编译器如何把 yield 和 await 拆成状态机。

三个场景：
1. def + yield（普通 generator）
2. async def + yield（async generator）
3. async def + await（协程）

每个场景都展示：原始写法 vs 编译器拆出来的状态机
"""

# ====================================================================
# 场景 1：def + yield → 状态机，return 给 for 循环
# ====================================================================

# --- 原始写法 ---
def original_gen():
    print("  step A")
    yield 1
    print("  step B")
    yield 2
    print("  step C")
    return "done"

# --- 编译器拆出来的状态机 ---
class GenStateMachine:
    def __init__(self):
        self.phase = 0
        self.result = None

    def resume(self):
        if self.phase == 0:
            print("  step A")
            self.phase = 1
            return ("yield", 1)          # yield 1 → 给调用者
        elif self.phase == 1:
            print("  step B")
            self.phase = 2
            return ("yield", 2)          # yield 2 → 给调用者
        elif self.phase == 2:
            print("  step C")
            self.result = "done"
            return ("finished", "done")  # return → StopIteration


# ====================================================================
# 场景 2：async def + yield → 状态机，return 给 async for
# ====================================================================

# --- 原始写法 ---
# async def original_async_gen():
#     print("  fetch start")
#     yield "event-1"
#     await asyncio.sleep(0)    ← 这里切给 event loop
#     print("  fetch done")
#     yield "event-2"

# --- 编译器拆出来的状态机 ---
class AsyncGenStateMachine:
    def __init__(self):
        self.phase = 0

    def resume(self, send_value=None):
        if self.phase == 0:
            print("  fetch start")
            self.phase = 1
            return ("yield", "event-1")       # yield → 给 async for 消费者
        elif self.phase == 1:
            # await asyncio.sleep(0) 编译成：
            future = "fake_future(sleep)"
            self.phase = 2
            return ("await", future)           # await → 给 event loop
        elif self.phase == 2:
            # event loop send 回来后继续
            print("  fetch done")
            self.phase = 3
            return ("yield", "event-2")       # yield → 给 async for 消费者
        elif self.phase == 3:
            return ("finished", None)


# ====================================================================
# 场景 3：async def + await → 状态机，return 给 event loop
# ====================================================================

# --- 原始写法 ---
# async def original_coro():
#     print("  connecting")
#     data = await fetch_url("http://example.com")
#     print(f"  got {data}")
#     result = await save_to_db(data)
#     print(f"  saved {result}")
#     return result

# --- 编译器拆出来的状态机 ---
class CoroStateMachine:
    def __init__(self):
        self.phase = 0
        self.data = None
        self.result = None

    def resume(self, send_value=None):
        if self.phase == 0:
            print("  connecting")
            # await fetch_url 编译成：
            future = "fake_future(fetch_url)"
            self.phase = 1
            return ("await", future)          # → event loop
        elif self.phase == 1:
            # event loop send 回来的值就是 fetch_url 的结果
            self.data = send_value
            print(f"  got {self.data}")
            # await save_to_db 编译成：
            future = "fake_future(save_to_db)"
            self.phase = 2
            return ("await", future)          # → event loop
        elif self.phase == 2:
            self.result = send_value
            print(f"  saved {self.result}")
            return ("finished", self.result)  # return → 给 await 的调用者


# ====================================================================
# 模拟运行
# ====================================================================

def simulate_for_loop(name, sm):
    """模拟 for 循环 / async for 驱动状态机"""
    print(f"\n{'=' * 50}")
    print(f"{name}")
    print(f"{'=' * 50}")

    send_val = None
    while True:
        status, value = sm.resume(send_val)

        if status == "yield":
            print(f"  [消费者拿到 yield]: {value}")
            send_val = None

        elif status == "await":
            print(f"  [event loop 拿到 future]: {value}")
            print(f"  [event loop: 挂起协程，去跑别的...]")
            print(f"  [event loop: 数据回来了，send 回去]")
            send_val = f"<{value} 的结果>"  # 模拟 event loop send 回结果

        elif status == "finished":
            print(f"  [结束，return 值]: {value}")
            break


def simulate_original_gen():
    """对比：用真正的 generator 跑"""
    print(f"\n{'=' * 50}")
    print(f"对照组：真正的 def + yield")
    print(f"{'=' * 50}")
    gen = original_gen()
    try:
        while True:
            val = next(gen)
            print(f"  [for 循环拿到]: {val}")
    except StopIteration as e:
        print(f"  [结束，return 值]: {e.value}")


if __name__ == "__main__":
    # 场景 1：def + yield 的状态机
    simulate_for_loop(
        "场景 1：def + yield → 状态机（yield 全给消费者）",
        GenStateMachine()
    )

    # 对照：真正的 generator
    simulate_original_gen()

    # 场景 2：async def + yield + await 的状态机
    simulate_for_loop(
        "场景 2：async def + yield + await → 状态机（yield 给消费者，await 给 event loop）",
        AsyncGenStateMachine()
    )

    # 场景 3：async def + await 的状态机
    simulate_for_loop(
        "场景 3：async def + await → 状态机（await 全给 event loop）",
        CoroStateMachine()
    )
