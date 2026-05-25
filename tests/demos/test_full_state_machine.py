"""
完整模拟：编译器拆状态机 + event loop 驱动 + 值的往返

展示三个完整场景，包括值怎么出去、event loop 怎么处理、值怎么回来
"""


# ====================================================================
# 场景 1：def + yield，for 循环驱动
#
# 原始代码：
#   def gen():
#       x = yield "请给我一个数"
#       yield f"你给了我 {x}"
#       return "结束"
# ====================================================================

class Scene1_GenStateMachine:
    """编译器拆出来的状态机"""
    def __init__(self):
        self.phase = 0
        self.x = None

    def send(self, value=None):
        if self.phase == 0:
            # x = yield "请给我一个数"
            self.phase = 1
            return "请给我一个数"           # yield 出去
            # ↑ 函数在这里真的 return 了，退出了

        elif self.phase == 1:
            # 从 yield 恢复，value 就是外部 send 进来的值
            self.x = value                  # ← 外部的值回来了
            self.phase = 2
            return f"你给了我 {self.x}"     # yield 出去

        elif self.phase == 2:
            raise StopIteration("结束")     # return → StopIteration


def run_scene1():
    print("=" * 60)
    print("场景 1：def + yield，for 循环驱动")
    print("=" * 60)

    sm = Scene1_GenStateMachine()

    # 第 1 次：启动，拿到第一个 yield
    val = sm.send(None)
    print(f"  for 循环拿到: {val!r}")

    # 第 2 次：send 一个值进去，拿到第二个 yield
    val = sm.send(42)                       # ← 42 就是 x 的值
    print(f"  for 循环拿到: {val!r}")

    # 第 3 次：send 继续，generator 结束
    try:
        sm.send(None)
    except StopIteration as e:
        print(f"  结束，return 值: {e.value!r}")

    # 对比真正的 generator
    print("\n  --- 对照组：真正的 generator ---")
    def real_gen():
        x = yield "请给我一个数"
        yield f"你给了我 {x}"
        return "结束"

    g = real_gen()
    print(f"  next(): {next(g)!r}")
    print(f"  send(42): {g.send(42)!r}")
    try:
        g.send(None)
    except StopIteration as e:
        print(f"  结束: {e.value!r}")


# ====================================================================
# 场景 2：async def + await，event loop 驱动
#
# 原始代码：
#   async def coro():
#       print("开始")
#       data = await fetch("http://example.com")   ← yield future 第1次
#       print(f"拿到 {data}")
#       result = await save(data)                   ← yield future 第2次
#       print(f"保存了 {result}")
#       return result
#
# 外部只需 result = await coro()，不管内部有几次 yield
# ====================================================================

class FakeFuture:
    """模拟 asyncio.Future"""
    def __init__(self, description):
        self.description = description
        self.result = None
        self.done = False

    def set_result(self, value):
        self.result = value
        self.done = True

    def __repr__(self):
        return f"Future({self.description})"


class Scene2_CoroStateMachine:
    """编译器拆出来的状态机"""
    def __init__(self):
        self.phase = 0
        self.data = None
        self.result = None

    def send(self, value=None):
        if self.phase == 0:
            print("    [协程内部] 开始")
            # data = await fetch("http://example.com")
            # 编译成：创建 future，yield 给 event loop
            future = FakeFuture("fetch(http://example.com)")
            self.phase = 1
            return future                   # ← yield future 给 event loop
            # 函数 return 了，退出了，栈清空

        elif self.phase == 1:
            # event loop send 回来的值 = future.result
            self.data = value               # ← "网页内容" 回来了
            print(f"    [协程内部] 拿到 {self.data}")
            # result = await save(data)
            future = FakeFuture(f"save({self.data})")
            self.phase = 2
            return future                   # ← yield future 给 event loop
            # 又 return 了

        elif self.phase == 2:
            self.result = value             # ← "保存成功" 回来了
            print(f"    [协程内部] 保存了 {self.result}")
            raise StopIteration(self.result)  # return result


def run_scene2():
    print("\n" + "=" * 60)
    print("场景 2：async def + await，event loop 驱动")
    print("  外部只需 result = await coro()")
    print("  内部有 2 次 yield future，event loop 自动处理")
    print("=" * 60)

    sm = Scene2_CoroStateMachine()

    print("\n  [event loop] 第 1 次 send(None) 启动协程")
    future = sm.send(None)
    print(f"  [event loop] 拿到: {future}")
    print(f"  [event loop] 我认识这是 Future → 挂起协程 → 去干别的")
    print(f"  [event loop] ... 网络请求回来了 ...")
    future.set_result("网页内容")

    print(f"\n  [event loop] 第 2 次 send({future.result!r}) 恢复协程")
    future2 = sm.send(future.result)
    print(f"  [event loop] 拿到: {future2}")
    print(f"  [event loop] 又是 Future → 挂起 → 去干别的")
    print(f"  [event loop] ... 数据库写入完成 ...")
    future2.set_result("保存成功")

    print(f"\n  [event loop] 第 3 次 send({future2.result!r}) 恢复协程")
    try:
        sm.send(future2.result)
    except StopIteration as e:
        print(f"  [event loop] 协程结束，return 值: {e.value!r}")
        print(f"  [event loop] 这个值就是 'result = await coro()' 拿到的")


# ====================================================================
# 场景 3：async def + yield + await，两条通道同时工作
#
# 原始代码：
#   async def stream():
#       yield "开始下载"                        ← 给 async for
#       data = await fetch("http://example.com") ← 给 event loop
#       yield f"下载完成: {data}"                ← 给 async for
#       await save(data)                         ← 给 event loop
#       yield "保存完成"                         ← 给 async for
# ====================================================================

class Scene3_AsyncGenStateMachine:
    """编译器拆出来的状态机"""
    def __init__(self):
        self.phase = 0
        self.data = None

    def send(self, value=None):
        if self.phase == 0:
            self.phase = 1
            return ("yield", "开始下载")              # → async for

        elif self.phase == 1:
            # await fetch
            future = FakeFuture("fetch")
            self.phase = 2
            return ("await", future)                   # → event loop

        elif self.phase == 2:
            self.data = value                          # ← event loop send 回来
            self.phase = 3
            return ("yield", f"下载完成: {self.data}")  # → async for

        elif self.phase == 3:
            # await save
            future = FakeFuture("save")
            self.phase = 4
            return ("await", future)                   # → event loop

        elif self.phase == 4:
            self.phase = 5
            return ("yield", "保存完成")               # → async for

        elif self.phase == 5:
            return ("finished", None)


def run_scene3():
    print("\n" + "=" * 60)
    print("场景 3：async def + yield + await，两条通道")
    print("=" * 60)

    sm = Scene3_AsyncGenStateMachine()

    send_val = None
    step = 0
    while True:
        step += 1
        status, value = sm.send(send_val)
        send_val = None

        if status == "yield":
            print(f"\n  第 {step} 步 → [async for 消费者拿到]: {value!r}")

        elif status == "await":
            print(f"\n  第 {step} 步 → [event loop 拿到 Future]: {value}")
            print(f"             [event loop: 挂起，去跑别的协程...]")
            print(f"             [event loop: 数据回来了]")
            value.set_result(f"{value.description} 的结果")
            send_val = value.result
            print(f"             [event loop: send({send_val!r}) 恢复]")

        elif status == "finished":
            print(f"\n  第 {step} 步 → [async generator 结束]")
            break


# ====================================================================
# 场景 4：多层 await 嵌套，yield from 一层层穿透
#
# 原始代码：
#   async def inner():
#       data = await fetch(url)    ← yield future
#       return data
#
#   async def middle():
#       result = await inner()     ← yield from 穿透
#       return result + "!"
#
#   async def outer():
#       final = await middle()     ← yield from 穿透
#       return final
# ====================================================================

class Inner_SM:
    def __init__(self):
        self.phase = 0

    def send(self, value=None):
        if self.phase == 0:
            print("        [inner] await fetch → yield future")
            future = FakeFuture("fetch")
            self.phase = 1
            return ("await", future)           # yield future → 穿透上去
        elif self.phase == 1:
            data = value
            print(f"        [inner] 拿到 {data!r}, return")
            raise StopIteration(data)          # return data


class Middle_SM:
    def __init__(self):
        self.phase = 0
        self.inner = Inner_SM()

    def send(self, value=None):
        if self.phase == 0:
            # result = await inner()
            # 编译成：调 inner.send()，如果返回 await，我也 return（穿透）
            status, val = self.inner.send(value)
            if status == "await":
                self.phase = 1
                print("      [middle] inner yield 了 future，我也 return 穿透")
                return ("await", val)          # ← 穿透！不是我的 future，是 inner 的
        elif self.phase == 1:
            # event loop send 回来，继续传给 inner
            try:
                status, val = self.inner.send(value)
                if status == "await":
                    return ("await", val)      # inner 还要继续 await
            except StopIteration as e:
                result = e.value + "!"
                print(f"      [middle] inner 结束，result={result!r}, return")
                raise StopIteration(result)


class Outer_SM:
    def __init__(self):
        self.phase = 0
        self.middle = Middle_SM()

    def send(self, value=None):
        if self.phase == 0:
            status, val = self.middle.send(value)
            if status == "await":
                self.phase = 1
                print("    [outer] middle 穿透了 future，我也 return 穿透")
                return ("await", val)
        elif self.phase == 1:
            try:
                status, val = self.middle.send(value)
                if status == "await":
                    return ("await", val)
            except StopIteration as e:
                final = e.value
                print(f"    [outer] middle 结束，final={final!r}, return")
                raise StopIteration(final)


def run_scene4():
    print("\n" + "=" * 60)
    print("场景 4：三层嵌套 await，yield from 一层层穿透")
    print("  outer → middle → inner → yield future")
    print("  future 穿透三层回到 event loop")
    print("=" * 60)

    sm = Outer_SM()

    print("\n  [event loop] send(None) 启动")
    status, future = sm.send(None)
    print(f"  [event loop] 拿到: {future}")
    print(f"  [event loop] 挂起，等数据...")
    future.set_result("response data")

    print(f"\n  [event loop] send({future.result!r}) 恢复")
    try:
        sm.send(future.result)
    except StopIteration as e:
        print(f"  [event loop] 最终结果: {e.value!r}")


# ====================================================================

if __name__ == "__main__":
    run_scene1()
    run_scene2()
    run_scene3()
    run_scene4()
