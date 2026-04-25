# 异步编程与操作系统底层原理 — 深度对话总结

## 1. 文件监听原理（OS 层）

操作系统提供内核级文件监听 API：
- Windows: ReadDirectoryChangesW + IOCP（I/O Completion Port）
- Linux: inotify + epoll
- macOS: FSEvents / kqueue

原理：所有文件操作必须经过内核（VFS 层），内核在处理写入时顺便检查是否有 watcher 注册，有则往事件队列写记录。不是轮询，是被动通知。

完整链条：
```
用户/程序修改文件
  → 文件系统驱动（NTFS/ext4/APFS）执行写入时检查 watcher
  → 生成事件写入内核事件缓冲区
  → OS 异步通知机制（IOCP/epoll/kqueue）
  → Node.js libuv / Python asyncio 事件循环
  → 应用层库（chokidar / watchdog）
  → 业务回调（清缓存、重载配置）
```

## 2. 硬件中断驱动 — OS 底层根本没有轮询

OS 底层不是轮询，是硬件中断驱动：
- 硬件（键盘、网卡、磁盘）完成操作后发送电信号到 CPU 中断引脚
- CPU 强制暂停当前指令，跳转到内核中断处理程序（IDT 表）
- 内核处理事件，唤醒等待的线程
- 全链路都是被动通知，没有任何一层在轮询

具体过程：
```
CPU 正在跑线程 B 的第 1000 条指令
  → 磁盘控制器：写入完成，拉高中断引脚 ⚡
  → CPU 强制跳转到内核中断处理函数
  → 内核：磁盘写入完成 → 检查 inotify watcher → 写事件 → 唤醒等待线程
  → CPU 回到线程 B 继续执行
  → 调度器轮到被唤醒的线程时，该线程的 read() 返回事件数据
```

## 3. 线程挂起的本质

"线程挂起"不是 CPU 停了，是内核把线程从运行队列移到等待队列：
- 运行队列：CPU 调度器轮流执行的线程列表
- 等待队列：不会被 CPU 执行的线程列表
- 唤醒 = 从等待队列移回运行队列（链表操作）
- CPU 永远在跑运行队列里的线程，等待队列里的线程完全不消耗 CPU

```
运行队列（CPU 轮流执行）：[线程 B] [线程 C] [线程 D]
等待队列（不消耗 CPU）：  [线程 A — 在等 inotify 事件]
```

线程锁（mutex）、文件监听（inotify）、epoll_wait 底层都用同一个原语：等待队列 + 唤醒。在 Linux 内核中统一使用 wait_queue_head_t 实现。

## 4. 协程的本质 — 编译器生成的状态机

协程不涉及 CPU 调度或内核，是编译器把函数拆成状态机：

核心机制：
- 每个 yield/await 是一个切片点，编译器在此处"切一刀"
- 编译后变成 switch-case 结构
- 局部变量从栈搬到堆上的结构体（所以 return 后不会丢失）
- await 的本质就是 return（函数退出），不是阻塞等待
- 恢复执行 = 再次调用同一个函数，switch 跳到上次记住的 case

C 语言视角：
```c
// 原始协程代码：
// async def my_coro():
//     a = 1
//     await something()   ← 切片点
//     b = a + 2
//     return b

struct coroutine_state {
    int phase;      // 当前执行到第几步
    int a;          // 局部变量（堆上，不在栈上）
    int b;
};

int coroutine_resume(struct coroutine_state *state) {
    switch (state->phase) {
        case 0:
            state->a = 1;
            state->phase = 1;       // 记住下次从 case 1 继续
            return YIELDED;         // yield = 普通的 return！
        case 1:
            state->b = state->a + 2;
            return COMPLETED;
    }
}
```

没有 OS 系统调用，没有线程切换，没有中断，没有特殊 CPU 指令。CPU 看到的就是普通的函数调用和 return。

> **重要澄清：CPython 实际并非编译为状态机**
>
> 上述 switch-case 状态机模型是 **Rust/C 编译协程的实现方式**，用于理解协程"暂停-恢复"的概念模型。
> CPython 的实际实现完全不同——每个函数有独立的 PyFrameObject，通过保存/恢复 f_lasti（指令指针）实现暂停恢复，
> 不存在编译期的 switch-case 转换。详见第 36 节。

## 5. Event Loop 的本质

Event Loop 不是单独的线程，就是主线程跑的 while 循环：

```python
# asyncio.run() 内部本质
def run(coro):
    loop = EventLoop()
    loop.runnable.append(coro)
    
    while not all_done:
        # 第一步：把能跑的协程都跑一遍
        while runnable:
            co = runnable.pop()
            co.resume()           # 普通函数调用！跑到 yield 就 return 回来
        
        # 第二步：检查定时器堆，到期的移入 runnable
        while scheduled and scheduled[0].when <= now():
            handle = heappop(scheduled)
            runnable.append(handle)
        
        # 第三步：epoll_wait 等 IO（唯一的线程挂起点）
        # 有活干就 timeout=0（不睡），否则睡到下一个定时器到期
        timeout = 0 if runnable else next_timer_delay()
        ready_fds = epoll_wait(io_waiting.keys(), timeout)
        for fd in ready_fds:
            runnable.append(io_waiting.pop(fd))
        
        # 注意：Future 不在这里轮询检查！
        # future.set_result() 触发回调 → 回调自动把协程塞进 runnable
        # 这是回调驱动，不是轮询驱动（详见第 27、33 节）
```

程序从头到尾就一个线程。调用 asyncio.run() 的那一刻，主线程就进入了这个 while 循环，直到所有协程跑完才出来。

## 6. 协程并发的真相

核心结论：
- 协程永远是顺序执行，一次只跑一个，没有并行，CPU 不会切换来跑
- "并发"不是同时执行代码，是同时等待 IO
- 异步 = "发起"和"拿结果"分开，不等结果就先做下一件事
- 省的是 IO 等待时间，CPU 做的活一样多
- 纯计算任务用协程完全没有并发效果，需要多线程/多进程

```
没有协程（同步）：
  CPU: [发请求A][====等A 200ms====][处理A][发请求B][====等B 200ms====][处理B]
  总耗时：400ms+

有协程（异步）：
  CPU: [发请求A][发请求B][===等AB===][处理A][处理B]
  网络:         [====A在飞====]
                [====B在飞====]
  总耗时：200ms+（省的是等待时间，CPU 做的活一样多）
```

类比：你去餐厅，同步 = 点一道菜等做好再点下一道（90分钟），异步 = 三道菜一起点然后坐着等（30分钟）。你还是一个人，一次只能端一盘菜，但不傻等一道做好才点下一道。

## 7. 协程并发只存在于 create_task

关键规则：
- 没有 create_task 就没有并发，就是顺序等待
- create_task 把协程注册到 event loop 的 runnable 列表
- gather 等待列表中所有协程完成
- 如果只有一个协程在跑，await 时效果等同于同步阻塞

```python
# 没有 create_task → 没有并发，串行等待
async def main():
    await func()          # 等它完，才走下一行
    await file_watcher()  # 等它完

# 有 create_task → 有并发
async def main():
    task1 = asyncio.create_task(func())           # 注册到 event loop
    task2 = asyncio.create_task(file_watcher())   # 注册到 event loop
    await asyncio.gather(task1, task2)             # 两个轮流跑
```

"并列"不是"同时执行"，是"都在 event loop 的待执行列表里"。event loop 一个个取出来跑，跑到 yield 就换下一个。

## 8. yield from / await 的嵌套穿透

- **await 链条底层的 yield（Future.__await__ 里的 `yield self`）** 会穿透所有层回到 event loop
- **async generator 里的 yield** 不会穿透到 event loop，只交给 async for 消费者
- 这是两条完全独立的通道：await 通道通向 event loop，yield 通道通向调用者
- yield from 自动转发，不是 switch 套 switch，是线性的函数调用链
- 100 层嵌套 yield 30 次 = 3000 次函数调用，但每次只是一个函数调用 + return，耗时微秒级
- 和 IO 等待（毫秒级）相比开销可忽略（差 4-5 个数量级）

## 9. 非阻塞 IO 的原理

操作系统提供非阻塞模式的系统调用：

```c
// 阻塞模式（默认）：
recv(fd, buf, len, 0);        // 线程挂起，等数据到达

// 非阻塞模式：
fcntl(fd, F_SETFL, O_NONBLOCK);
int n = recv(fd, buf, len, 0);     // 不阻塞！
if (n == -1 && errno == EAGAIN) {
    // 没数据，但函数立刻返回了，返回 EAGAIN 表示"你先忙别的"
}
```

配合 epoll 使用：
1. send() 把数据交给内核（瞬间完成）→ 内核交给网卡 → 网卡发出去
2. 注册 fd 到 epoll
3. 去干别的事（发其他请求）
4. epoll_wait() 统一等所有 fd
5. 哪个先就绪就先处理

CPU 真正干活的只有 send 和 recv 那一瞬间，中间网络传输过程 CPU 完全不参与。

## 10. Claude Code 的热重载机制

Claude Code 使用 chokidar（Node.js 文件监听库）实现全自动重载，不需要手动 reload：

- **Settings（settings.json）**：chokidar 监听 + 1 秒稳定期 + 清缓存 + 重新从磁盘读取
- **Skills（SKILL.md）**：chokidar 监听 skills/ 目录（depth:2）+ 300ms 防抖 + clearSkillCaches + clearCommandsCache
- **MCP Server**：settings 变更触发 MCP 连接重新同步 + MCP 协议本身的 tools/list_changed 通知
- **CLAUDE.md**：每轮重新从磁盘读取，无需 watcher

全部自动重载，核心靠 chokidar 文件监听 + MCP 协议通知。

## 11. 应用层对应库

- Node.js：chokidar（封装 fs.watch，底层用 libuv 调用 OS API）
- Python：watchdog（Windows 用 ReadDirectoryChangesW，Linux 用 inotify，macOS 用 FSEvents）
- 底层原理完全一致，都是注册到内核的文件系统事件通知机制

## 12. Future 的设计意义

在当前 agent 架构中，await future 时如果没有其他 create_task 的协程，效果等同于 input() 阻塞。但使用 future 的意义是为未来扩展留空间——如果以后加了文件监听、心跳、超时检测等 create_task 协程，不用改现有代码就能并发。架构上不会把路堵死。

## 13. create_task 的行为验证（实验证明）

### 实验代码与结论

通过实际测试验证了 create_task 的行为。**注意：两个测试在同一个 event loop 中运行**（由一个 main 函数依次 await），这是理解输出结果的前提：

```python
import asyncio

async def bg_task():
    print("bg_task 执行了！")
    return "done"

# 测试1：纯 while 轮询，不 yield → task 永远不执行
async def test1():
    task = asyncio.create_task(bg_task())
    for i in range(100000):
        if task.done():
            print(f"第 {i} 次检查：done!")
            return
    print(f"检查了 100000 次，task.done() 始终是 False")

# 测试2：轮询 1000 次后 yield → task 立刻执行
async def test2():
    task = asyncio.create_task(bg_task())
    for i in range(1000):
        if task.done():
            break
    else:
        print(f"yield 之前检查了 1000 次，全是 False")
    await asyncio.sleep(0)  # yield 一下
    print(f"yield 之后：task.done() = {task.done()}")

async def main():
    print("=== 测试1：纯轮询，不 yield ===")
    await test1()       # test1 return 后，它的 task 仍在 event loop 中待执行
    print("\n=== 测试2：轮询 1000 次后 yield ===")
    await test2()       # test2 的 sleep(0) yield 时，两个 task 都有机会执行

asyncio.run(main())
```

实际运行结果：
```
=== 测试1：纯轮询，不 yield ===
检查了 100000 次，task.done() 始终是 False

=== 测试2：轮询 1000 次后 yield ===
yield 之前检查了 1000 次，全是 False
bg_task 执行了！
bg_task 执行了！
yield 之后：task.done() = True
```

### 关键发现

1. create_task 只是把协程放进 event loop 的 runnable 列表，不会立刻执行
2. 不 yield 就永远轮不到它（测试1 证明：10万次检查全是 False）
3. yield 之后 event loop 轮一圈，task 才有机会执行（测试2 证明）
4. "bg_task 执行了！"打印两次：test1 的 task 没有被 await 也没有被 cancel，test1 return 后它仍在 event loop 的待执行列表中。当 test2 的 `await asyncio.sleep(0)` yield 出去时，event loop 一口气执行了两个待执行的 task（test1 遗留的 + test2 自己创建的）。**如果两个测试用独立的 `asyncio.run()` 运行，test1 的 task 会在 event loop 关闭时被取消，不会出现"打印两次"的现象。**
5. task 不会作废，只要 event loop 还活着就一定会执行，但在哪个 yield 的缝隙执行不确定
6. 没有 IO 等待的场景，create_task 和同步调用基本没区别
7. 协程是"协作式"调度——必须主动让出（yield），别人才能跑，不像线程是 OS 强制打断

## 14. 同步 generator 与 async 协程的根本区别 — 谁来调度下一步

### 核心区别

两者都是"把函数拆成步骤一步步执行"，区别在于**谁来调度下一步**：

- **同步 yield** → 值直接回到**调用者**（调用者只管这一个 generator，中间不可能插别的）
- **async await** → 控制权回到 **event loop**（event loop 管所有协程，可以穿插执行别的协程）

### yield from vs await

语法长得像，运行机制完全不同：

```python
# 同步 generator — yield from 直接转发给调用者，不经过任何中间人
def run_agent_loop():
    result = yield from some_generator()

# async 协程 — await 编译后会回到 event loop，event loop 可以穿插别的
async def agent_loop():
    result = await some_coroutine()
```

### async for 的执行机制

```python
async for ev in self._impl(self):
    yield ev
```

这段代码看起来像调用者在直接迭代，但实际上 `async for` 每次迭代底层都是 `await __anext__()`。**但 await __anext__() 是否真的让出给 event loop，取决于 async generator 内部有没有 await：**

```python
# ✗ 内部没有 await → __anext__() 瞬间完成 → 不让出给 event loop
async def gen_no_await():
    yield 1  # async for 一口气拿完所有值，其他协程没机会跑
    yield 2

# ✓ 内部有 await → __anext__() 会挂起 → 让出给 event loop
async def gen_with_await():
    await asyncio.sleep(0)  # ← 这里让出
    yield 1
    await asyncio.sleep(0)  # ← 这里让出
    yield 2
```

只有 async generator 内部有 `await`（且 await 的对象未完成）时，event loop 才有机会在迭代间隙调度其他协程。纯 yield 不会给 event loop 任何机会。

### 混合架构中 create_task 的有效性

判断 create_task 有没有用，就顺着调用链往上看：**上面有没有 await 回到 event loop？**

```
async def agent_loop()          ← 协程层，有 event loop
    ├── create_task(...)        ← 有用，因为下面有 await
    └── await consume_events()  ← 协程层，内部有 await
            └── for event in gen:  ← 同步迭代 run_agent_loop
                    └── run_agent_loop 是 def，不是 async def
                        它 yield 事件，但跟 event loop 无关
```

- **async 层**（agent_loop, consume_events）：create_task 有用，因为有 await 点让 event loop 穿插执行
- **sync generator 层**（run_agent_loop）：不能也不需要 create_task，它通过 yield 把事件抛给 consume_events

async 的"魔法"：你写的代码看起来是顺序的、直接的，但运行时每个 await 点都会绕一圈 event loop。编译器帮你把"看起来直接调用"变成了"经过 event loop 调度"。

## 15. asyncio.to_thread 的完整原理 — 同步阻塞函数的异步化

### 为什么需要 to_thread

`input()` 是 C 语言写的内置函数，内部没有 yield/await，就是一个死等的系统调用 `read(STDIN)`。如果直接在协程里调用，整个 event loop 卡死。`asyncio.to_thread` 把阻塞调用搬到线程池，让 event loop 在等待期间还能调度其他协程。

### to_thread 源码解析

```python
async def to_thread(func, /, *args, **kwargs):
    loop = events.get_running_loop()
    ctx = contextvars.copy_context()
    func_call = functools.partial(ctx.run, func, *args, **kwargs)
    return await loop.run_in_executor(None, func_call)
```

`run_in_executor` 是普通函数（不是 async def），调用就立刻执行：

```python
def run_in_executor(self, executor, func, *args):
    # executor 为 None → 创建默认 ThreadPoolExecutor
    future = wrap_future(executor.submit(func, *args), loop=self)
    return future  # 返回 asyncio.Future
```

### wrap_future 和 _chain_future — 线程与 event loop 的桥梁

`wrap_future` 创建一个 asyncio.Future，通过 `_chain_future` 绑定：当线程池的 Future 完成时，通过 `call_soon_threadsafe` 通知 event loop 设置 asyncio.Future 的结果。

```python
def _chain_future(source, destination):
    def _call_set_state(source):
        # 跨线程 → 用 call_soon_threadsafe 通知 event loop
        dest_loop.call_soon_threadsafe(_set_state, destination, source)
    source.add_done_callback(_call_set_state)
```

### Future.__await__ 的 yield 机制

```python
def __await__(self):
    if not self.done():
        yield self    # 把自己抛给 event loop，协程暂停
    return self.result()  # future done 后，返回结果给 await 表达式
```

`yield self` 和 `return self.result()` 的接收者不同：
- `yield self` → 给 event loop（调度器用 next()/send() 驱动，不用 await）
- `return self.result()` → 给 await 表达式（你的代码拿到的值）

### 完整执行链条

```
answer = await asyncio.to_thread(input, prompt)
  → await 启动 to_thread 协程（async def 不 await 就不执行）
    → run_in_executor 立刻执行：创建线程，input() 已经在线程里跑了
    → 返回 asyncio.Future
    → await future → future.__await__() → yield self → 穿透到 event loop
    → event loop 记下"这个协程在等这个 future"，去跑别的协程
    
    ...线程里 input() 等用户敲键盘...
    
    → 用户敲回车 → input() 返回 "y"
    → 线程调 call_soon_threadsafe(future.set_result, "y")
    → event loop 下一圈执行 set_result → future done
    → event loop 调 send() 恢复协程
    → future.__await__ 从 yield 后继续 → return self.result() → "y"
    → to_thread return "y"
  → answer = "y"
```

### 两层 await 的区别

```python
answer = await asyncio.to_thread(input, prompt)  # 外层：启动 to_thread 协程
# to_thread 内部：
return await loop.run_in_executor(None, func_call)  # 内层：等线程完成的 future
```

- 外层 await：启动 to_thread 这个协程，让函数体开始执行
- 内层 await：等待线程池返回的 future

## 16. 两种 yield 的本质区别 — 业务层 vs 调度层

### 业务层 yield（async generator）

```python
yield PermissionRequest(future=future)  # 抛给 async for 的消费者
```
接收者是你的代码（consume_events），拿到的是业务对象，没有调度能力。

### 调度层 yield（Future.__await__）

```python
yield self  # 抛给 event loop
```
接收者是 event loop（调度器），拿到 future 对象后可以暂停当前协程、去跑别的协程。event loop 用 next()/send() 驱动，不用 await。

### 关键区别

- event loop 拿到 yield → 有调度权，可以暂停协程去跑别的
- 你的代码拿到 yield → 只能处理值，没有调度能力，卡住就是真的卡住
- 你的代码要把控制权还给 event loop，只能通过 await（内部最终 yield 到 event loop）

### await 的行为规则

await 内部碰到 yield → 协程暂停，event loop 自由。没有 yield → 一路执行到底，不暂停。这就是"协作式调度"——协程主动让出（yield），别人才能跑。

## 17. 多层嵌套 yield 的状态机拆解 — 详细图解

### 核心规则

- yield 是切刀，有 yield 才切成不同的 case
- 没有 yield 的代码不管多复杂，都在同一个 case 里一口气跑完
- while/for 循环变成 goto 跳转，不是嵌套 while
- 每层是独立的状态机（struct + switch），外层通过调用内层的 resume 来驱动
- 每次 resume 只走到一个 yield 就 return，等外部下次再调用

### 完整示例：双层循环 + 嵌套 async for

```python
async def inner():
    yield "x"
    yield "y"

async def outer():
    count = 0
    while count < 2:
        msg = f"round-{count}"
        yield msg                    # yield 1
        async for val in inner():
            combined = f"{count}-{val}"
            yield combined           # yield 2
            print(f"sent {combined}")
        print(f"round {count} done")
        count += 1
    yield "all-done"                 # yield 3
    print("finished")
```

### 拆解后的状态机（C 语言视角）

```c
// inner 状态机 — 简单，2 个 yield = 3 个 case
struct inner_state { int phase; };
char* inner_resume(inner_state *s) {
    switch (s->phase) {
        case 0: s->phase = 1; return "x";
        case 1: s->phase = 2; return "y";
        case 2: return FINISHED;
    }
}

// outer 状态机 — 3 个 yield = 3 个 case + goto 跳转模拟循环
struct outer_state { int phase; int count; char* val; inner_state *inner; };
char* outer_resume(outer_state *s) {
    switch (s->phase) {
        case 0:                          // 函数入口
            s->count = 0;
        case_while:                      // while 循环头
            if (s->count >= 2) goto case_yield3;
            s->phase = 1;
            return format("round-%d", s->count);   // yield 1

        case 1:                          // yield 1 之后回来
            s->inner = create_inner();
        case_for:                        // async for 循环头
            s->val = inner_resume(s->inner);
            if (s->val == FINISHED) goto case_for_end;
            s->phase = 2;
            return format("%d-%s", s->count, s->val);  // yield 2

        case 2:                          // yield 2 之后回来
            printf("sent %d-%s", s->count, s->val);
            goto case_for;               // async for 下一轮

        case_for_end:                    // async for 结束
            printf("round %d done", s->count);
            s->count++;
            goto case_while;             // while 下一轮

        case_yield3:                     // while 结束
            s->phase = 3;
            return "all-done";           // yield 3

        case 3:                          // yield 3 之后回来
            printf("finished");
            return FINISHED;
    }
}
```

### 逐次调用执行流程

```
调用1 → case 0 → count=0 → case_while(0<2) → return "round-0"
调用2 → case 1 → create_inner → case_for → inner→"x" → return "0-x"
调用3 → case 2 → print("sent 0-x") → goto case_for → inner→"y" → return "0-y"
调用4 → case 2 → print("sent 0-y") → goto case_for → inner→FINISHED
         → case_for_end → print("round 0 done") → count=1
         → goto case_while(1<2) → return "round-1"
调用5 → case 1 → create_inner → case_for → inner→"x" → return "1-x"
调用6 → case 2 → print("sent 1-x") → goto case_for → inner→"y" → return "1-y"
调用7 → case 2 → print("sent 1-y") → goto case_for → inner→FINISHED
         → case_for_end → print("round 1 done") → count=2
         → goto case_while(2>=2) → case_yield3 → return "all-done"
调用8 → case 3 → print("finished") → FINISHED
```

### 关键观察

1. 调用4 和调用7：inner 结束后没有 return，而是 goto 跳到 while 下一轮，继续跑到下一个 yield 才 return。中间跨了多个 goto 和 printf，但因为没有 yield，全在一次调用里跑完
2. 外层只有 3 个 yield 对应的 case（1、2、3），循环用 goto 实现，不是展开成 N 个 case
3. 内层是独立的状态机对象，外层通过 inner_resume() 驱动它，每次只走一步
4. 真正的 while 循环只在最外层的 event loop，中间层全是 switch + goto + return

## 18. 状态机大小 vs 执行次数 — 常见误解澄清

### 状态机大小（代码量）= yield 个数，是固定的

```python
for i in range(1000000):    # 百万次循环
    yield i                  # 只有 1 个 yield
```

拆出来只有 2 个 case，不是一百万个。循环用 goto 跳转实现，不展开。

### 多层嵌套也不会展开

```python
async def layer4():
    for i in range(3):
        async for val in layer5():
            yield val
```

layer4 的状态机只有 2 个 case（case_for + case 2），不管 layer5 内部有多少个 yield。外层对内层是黑盒调用：`inner_resume()` 调一次拿一个值，FINISHED 就结束。

### "展开"的错觉

执行流程看起来重复：
```
调用2 → case_for → inner→"x" → return
调用3 → case 2 → goto case_for → inner→"y" → return
调用4 → case 2 → goto case_for → inner→FINISHED
```

但这不是代码展开，是**同一段代码被反复执行**。就像 while 循环跑 3 次不是把循环体复制 3 份，是同一段代码跑 3 遍。case_for 和 case 2 只写了一次，通过 goto 反复进入。

### 性能总结

- 状态机**大小** = yield 个数（固定，通常几个到十几个）
- 状态机**执行次数** = yield 个数 × 循环次数（可以很大）
- 嵌套 N 层 = 每次 resume N 个函数调用
- 每次函数调用 = switch 跳转 + return，微秒级
- 5 层嵌套，每层 3 次循环 = 3⁵ = 243 次 resume × 5 层 = 1215 次函数调用 ≈ 1 毫秒
- 跟 IO 等待（毫秒到秒级）比完全可以忽略

## 19. async def 的 yield + return 限制

### Python 的规则

- `def` + `yield` + `return value` → 合法（普通 generator），return 值通过 StopIteration.value 传递
- `async def` + `yield` + `return value` → **语法错误**（async generator 不允许）
- `async def` + `return value`（无 yield）→ 合法（协程）
- `async def` + `yield`（无 return value）→ 合法（async generator）

### Future.__await__ 为什么能 yield + return

```python
def __await__(self):        # 普通 def，不是 async def
    if not self.done():
        yield self
    return self.result()
```

因为它是普通 `def`，不是 `async def`，所以 yield + return value 合法。

### AsyncGenWithResult 存在的原因

正是因为 `async def` 不能同时 yield 事件流和 return 最终值，所以用 `set_result()` 手动设置返回值来绕过这个限制。本质上就是一个 async generator + 手动 result slot。

## 20. await future 的死锁陷阱

同一个协程里 await 自己创建的 future 是死锁：

```python
future = asyncio.Future()
await future          # 暂停了，下面的代码永远跑不到
future.set_result(1)  # 永远执行不到
```

`set_result` 必须来自"别的地方"——另一个协程、线程回调、或 `call_soon_threadsafe`。因为 `await future` 的 yield 是直接给 event loop 的，当前协程被挂起，只有外部 set_result 后 event loop 才会恢复它。

## 21. 状态机中的循环与函数调用 — case_for 不是步骤

### 核心区分：case 编号 vs goto 标签

```c
case 0:      // ← 有编号，对应一个 yield，是真正的"步骤"
case 1:      // ← 有编号，对应一个 yield，是真正的"步骤"
case_for:    // ← 没有编号，是 goto 跳转目标，不是步骤
case_end:    // ← 没有编号，也是 goto 跳转目标
```

只有 yield 才会产生一个新的 case 编号。`case_for` 是循环头的跳转标签，每次进来都执行 `inner_resume()`，但它本身不是一个独立步骤。

### 循环内的 yield — 同一个 case 被多次执行

```python
async def example():
    yield "before"          # yield 1
    for i in range(3):
        x = i * 10
        yield f"item-{x}"  # yield 2（同一个 yield，但会被执行 3 次）
        print(f"done {i}")
    yield "after"           # yield 3
```

拆开后的状态机：

```c
struct state { int phase; int i; int x; };

char* resume(state *s) {
    switch (s->phase) {
        case 0:                              // 入口
            s->phase = 1;
            return "before";                 // yield 1

        case 1:                              // yield 1 之后回来
            s->i = 0;
        case_for:                            // 循环头（标签，不是 case）
            if (s->i >= 3) goto case_3;
            s->x = s->i * 10;
            s->phase = 2;
            return format("item-%d", s->x);  // yield 2

        case 2:                              // yield 2 之后回来
            printf("done %d", s->i);
            s->i++;
            goto case_for;                   // 跳回循环头

        case_3:                              // 循环结束
            s->phase = 3;
            return "after";                  // yield 3

        case 3: return FINISHED;
    }
}
```

### 逐次调用执行流程

```
调用1: case 0 → return "before"
调用2: case 1 → i=0 → case_for(0<3) → x=0 → return "item-0"
调用3: case 2 → print("done 0") → i=1 → goto case_for(1<3) → x=10 → return "item-10"
调用4: case 2 → print("done 1") → i=2 → goto case_for(2<3) → x=20 → return "item-20"
调用5: case 2 → print("done 2") → i=3 → goto case_for(3>=3) → goto case_3 → return "after"
调用6: case 3 → FINISHED
```

调用 3、4、5 都进入 `case 2`，同一个 case 被执行了 3 次。

### 多层嵌套时 case_for 的行为

```c
// A 的状态机
char* A_resume(A_state *s) {
    switch (s->phase) {
        case 0:
            s->inner_b = create_B();
        case_for:                            // ← 标签，不是步骤
            char* val = B_resume(s->inner_b); // 调用内层
            if (val == FINISHED) goto case_end;
            s->phase = 1;
            return val;

        case 1:                              // yield 之后回来
            goto case_for;                   // 直接跳回循环头
        case_end: return FINISHED;
    }
}
```

`case 1 → goto case_for → B_resume()` 这三步是**一次 resume 里连续执行的**，不是三次调用。就像普通代码里写 goto 跳到前面一行一样，瞬间完成。

### 关键结论

- case 的数量 = yield 的数量（固定，编译时确定）
- 调用次数 = yield 的数量 × 循环次数（运行时决定）
- 循环用 goto 跳回循环头实现，最后一步的 case 通过 goto case_for 回到开头
- case_for 是跳转标签不是步骤，`goto case_for` 和 `B_resume()` 在同一次调用里瞬间完成
- 多层嵌套时，每层记住自己的 phase，不会重头开始

## 22. async 与 sync 的根本执行差异 — 栈 vs 状态机跳转

### sync：函数留在栈上，等内层返回

```
A 调 B → B 调 C → C 跑完 return → B 继续 → B return → A 继续
全程 A、B 都在栈上，没有退出过。
```

函数调用是嵌套的，外层一直等着，栈帧一直存在。

### async：每次 yield 全部退出，栈清空

```
A 调 B → B 调 C → C yield → B return → A return → event loop
A B C 全部退出，栈清空。状态保存在堆上的 struct 里。
```

恢复时从 event loop 重新进入，每层 switch 跳到自己的 phase，一路跳转到最内层：

```
event loop → A_resume()
  → switch(phase=2) → case 2 → goto case_await → B_resume()
    → switch(phase=2) → case 2 → goto case_await → C_resume()
      → switch(phase=2) → case 2 → goto case_await → D_resume()
        → switch(phase=2) → case 2 → goto case_await → E_resume()
          → switch(phase=3) → case 3 → 真正的业务代码 → yield
```

外面四层全是 `switch → goto → 调内层`，只有最内层在干活。

### await 自动生成转发代码

你写 `await B()` 时没有手动 yield，但编译器在每一层自动生成了转发：

```c
// 每一层 await 编译后都有这段
val = inner_resume();
if (val != FINISHED) {
    s->phase = N;
    return val;          // ← 编译器自动加的，把内层的 yield 往上抛
}
```

这就是 yield 能从最内层穿透到 event loop 的原因——不是你手动抛的，是每层 await 帮你生成了 `return val`。

### 外层在等待期间的行为

内层 yield 多次时，外层的 phase 不变，每次进来执行同一个 case：

```
E yield 第1次: A(phase=2) B(phase=2) C(phase=2) D(phase=2) E(phase=0→1)
E yield 第2次: A(phase=2) B(phase=2) C(phase=2) D(phase=2) E(phase=1→2)
E yield 第3次: A(phase=2) B(phase=2) C(phase=2) D(phase=2) E(phase=2→3)
E FINISHED:    A(phase=2) B(phase=2) C(phase=2) D(phase=2→3) E=FINISHED
```

外层重复执行的只是 `case 2 → goto case_await → 调内层 resume`，纯跳转，没有业务代码。内层 FINISHED 后，外层的 phase 才推进，继续自己的业务逻辑。

### phase 的产生规则

- 每个 yield 产生一个 phase
- 每个 await（内部可能 yield）产生一个 phase
- 没有 yield 也没有 await 的函数只有 1 个 phase，一口气跑完
- phase 总数 = yield 次数 + await 次数 + 1（入口）

### 为什么 sync 里不能调 async

async 的 yield 需要一路 return 到 event loop。每层 await 自动生成 `return val` 转发。但 sync 函数没有 await，不会生成转发代码，yield 传不上去，调度链断裂。所以 async 可以调 sync，sync 不能调 async。

> **注意：以上"栈清空 + switch 跳转"描述的是编译型协程（Rust/C）的行为模型。**
> CPython 中栈帧（PyFrameObject）在 yield 后并不销毁，而是保留在堆上，f_lasti 记录暂停位置。
> 恢复时直接跳回 f_lasti 继续执行，不需要 switch-case 跳转。概念上效果相同（暂停-恢复），但实现机制不同。
> 详见第 36 节。

## 23. async for 的调度行为 — 实验验证（勘误）

### async for 的编译器展开

```python
async for val in gen():
    print(val)

# 等价于：
while True:
    try:
        val = await gen.__anext__()   # ← 每次都有 await
    except StopAsyncIteration:
        break
    print(val)
```

### `__anext__()` 返回的不是值，是可等待对象

```python
async def gen():
    yield 1

ag = gen()
result = ag.__anext__()
# type(result) → async_generator_asend
# 不是 int，不是直接的值！
# inspect.isawaitable(result) → True
# inspect.iscoroutine(result) → False
```

`__anext__()` 本身是同步的，立即返回一个 `async_generator_asend` 对象。这个对象实现了 `__await__` 协议。

### ~~关键发现~~ 勘误：纯同步 async generator 不会回 event loop

之前认为 `async_generator_asend` 会强制 yield 回 event loop，**实验证明这是错误的**。

实验验证：

```python
async def gen():
    yield 1    # 纯同步，没有 await
    yield 2

async def background():
    for i in range(5):
        print(f"[后台] tick {i}")
        await asyncio.sleep(0.05)

async def main():
    task = asyncio.create_task(background())
    async for val in gen():
        print(f"got {val}")
    await task

# 实际输出：
# got 1
# got 2
# [后台] tick 0    ← async for 全部跑完后才轮到后台协程
# [后台] tick 1
# ...
```

**后台协程完全没有在 async for 迭代间隙执行。** `async_generator_asend.__await__` 在 generator 内部没有 await 时，`__anext__()` 瞬间完成（raise StopIteration(value)），不会 yield 回 event loop。

### 正确结论：是否让出取决于 generator 内部有没有 await

```python
# ✗ 纯 yield → __anext__ 瞬间完成 → 不让出 → 后台协程没机会
async def gen_sync():
    yield 1
    yield 2

# ✓ yield + await → __anext__ 会挂起 → 让出 → 后台协程能跑
async def gen_async():
    await asyncio.sleep(0)  # ← 这里让出给 event loop
    yield 1
    await asyncio.sleep(0)
    yield 2
```

### await 和 yield 是两条独立通道

```
await future  → Future.__await__ → yield self → event loop 拿到 → 挂起协程
yield value   → async for 消费者拿到 value → event loop 完全不知道
```

即使你 `yield` 一个 Future 对象，event loop 也不会认识它——它只是 async for 消费者收到的一个普通 Python 对象。只有通过 `await` 走到 `Future.__await__` 里的隐藏 yield，event loop 才能识别并挂起协程。

### 总结

- `async for` 每次迭代 = `await __anext__()`
- 但 `await __anext__()` 是否让出，取决于 generator 内部有没有未完成的 await
- 纯 yield 的 async generator 和同步 for 循环行为一致，不会给 event loop 调度机会
- 要让 create_task 的协程在 async for 间隙执行，generator 内部必须有 await

## 24. event loop 如何根据 yield 值决定调度 — Future 挂起机制

### event loop 检查 yield 值的类型

event loop（具体是 Task.__step）拿到 yield 出来的值后，根据类型决定怎么处理：

```python
# Task.__step 内部逻辑（简化，完整版见第 33 节）
value = coroutine.send(None)

if hasattr(value, '_asyncio_future_blocking') and value._asyncio_future_blocking:
    # 是 Future → 挂起协程，等 set_result 后再恢复
    value.add_done_callback(self.__wakeup)

elif value is None:
    # 裸 yield（如 await asyncio.sleep(0) 最终的 yield None）→ 让出一轮，下轮继续
    self._loop.call_soon(self.__step)

else:
    # 其他任何东西 → 报错！不是合法的 yield 值
    raise RuntimeError(f'Task got bad yield: {value!r}')
```

- `yield Future` → Task.__step 认出 → 挂起协程 → 等别的地方 set_result → 才恢复
- `yield None` → 让出一轮，下轮继续
- `yield 其他值` → **RuntimeError**（只有上述两种是合法的）

### await future 的完整暂停-恢复流程

```python
async def example():
    x = 1
    result = await future    # yield self 出去，暂停
    print(result)            # 恢复后才执行
```

执行过程：

```
第1次: event loop → example.send(None)
  → x = 1
  → await future → future.__await__() → yield self → return Future 给 event loop
  → example 退出，栈清空

... event loop 拿到 Future，注册回调，去跑别的协程 ...
... 某个地方（线程/其他协程）调用 future.set_result("hello") ...

第2次: event loop → example.send("hello")
  → 从 yield 恢复 → result = "hello"
  → print("hello")
  → return FINISHED
```

### async for 中值的传递路径（勘误）

async for 的值**不经过 event loop**，而是被 `async_generator_asend` 直接截获：

```
gen: yield 1
  → async_generator_asend.__next__() 驱动 gen
  → gen yield 1 → asend 截获 → raise StopIteration(1)
  → await asend 瞬间完成 → consumer 拿到 val = 1
  → 全程在一次 task.__step() 里，event loop 没有介入
```

只有 async generator 内部有 `await`（且未完成）时，值的传递才会绕经 event loop：

```
gen: await asyncio.sleep(0) → yield future → event loop 介入 → 恢复后 yield 1
  → asend 拿到 1 → consumer 拿到 val = 1
```

### 为什么 asyncio.sleep(1) 会真的等 1 秒

```python
result = await asyncio.sleep(1)
```

内部流程：
1. sleep 创建一个 Future
2. 注册 1 秒后的定时器回调：`loop.call_later(1, future.set_result, None)`
3. `await future` → `yield self` → Future 到达 event loop
4. event loop 认出 Future → 挂起协程 → 去跑别的
5. 1 秒后定时器触发 → `future.set_result(None)`
6. event loop 恢复协程 → `result = None`

协程不是在"等"1 秒，是被 event loop 挂起了 1 秒。期间 event loop 自由调度其他协程。

## 25. call_soon_threadsafe — 跨线程往事件循环塞回调

### 用途

`call_soon_threadsafe` 专门用于从**其他线程**往事件循环线程塞回调。名字里的 `threadsafe` 就说明了它的唯一用途——跨线程通信。

### 与 call_soon 的区别

| 方法 | 从哪里调 | 做什么 |
|------|---------|--------|
| `loop.call_soon(fn)` | 事件循环线程内部 | 塞回调到 `_ready` 队列 |
| `loop.call_soon_threadsafe(fn)` | **任意其他线程** | 塞回调到 `_ready` 队列 **+ 写 self-pipe 唤醒 epoll_wait** |

事件循环线程可能正挂在 `epoll_wait` 里睡觉。`call_soon` 只往队列里放东西，不会叫醒它。`call_soon_threadsafe` 多做一步：往 self-pipe 写一个字节，这是个 IO 事件，`epoll_wait` 立刻返回。

### CPython 源码

```python
def call_soon_threadsafe(self, callback, *args, context=None):
    handle = events._ThreadSafeHandle(callback, args, self, context)
    self._ready.append(handle)     # GIL 保证原子性，无需额外锁
    self._write_to_self()          # 写 self-pipe 唤醒 epoll_wait
    return handle
```

没有显式锁。`collections.deque` 的 `append` 和 `popleft` 在 CPython GIL 保护下是原子的。多个线程同时 `call_soon_threadsafe`，由 GIL 串行化。

### 回调 vs 协程

回调和协程是两种完全不同的东西：

| | 回调（callback） | 协程（coroutine） |
|---|---|---|
| 能 `await` 吗 | 不能 | 能 |
| 能中途让出吗 | 不能，必须执行到 return | 能，每个 await 都能让出 |
| 阻塞事件循环 | `time.sleep` 会冻结一切 | `await asyncio.sleep` 只暂停自己 |

回调是普通函数，事件循环没有任何机会中途暂停它。如果回调里用了 `time.sleep(0.3)`，整个事件循环线程冻住 0.3 秒，所有协程都被饿死。

### 正确用法

只塞瞬间完成的回调：

```python
loop.call_soon_threadsafe(flag.set)        # 纳秒级，正确
loop.call_soon_threadsafe(print, "hi")     # 微秒级，正确
loop.call_soon_threadsafe(heavy_work)      # 冻住事件循环，错误
```

## 26. GIL（Global Interpreter Lock）

CPython 的一把全局锁。同一时刻只有一个线程能执行 Python 字节码。

多个线程"同时"调用 `deque.append`，GIL 保证不会交叉执行——一个做完另一个才能做。所以 `call_soon_threadsafe` 不需要额外加锁，靠 GIL 就够了。

## 27. _run_once 的快照机制 — CPython 事件循环核心

### run_forever 就是个 while 循环

```python
def run_forever(self):
    while True:
        self._run_once()
```

### _run_once 内部流程

```python
def _run_once(self):
    # 1. 检查定时器堆：到期的移入 _ready
    now = self.time()
    while self._scheduled:
        handle = self._scheduled[0]
        if handle._when > now:
            break
        handle = heapq.heappop(self._scheduled)
        self._ready.append(handle)

    # 2. 计算 epoll_wait 超时
    if self._ready:
        timeout = 0          # 有活干，不睡
    elif self._scheduled:
        timeout = 下一个定时器到期时间 - now   # 睡到定时器到期
    else:
        timeout = 很长        # 啥都没有，一直睡

    # 3. epoll_wait
    event_list = self._selector.select(timeout)

    # 4. 处理 IO 事件（加入 _ready）

    # 5. 快照执行 _ready 队列
    ntodo = len(self._ready)          # ← 快照！
    for i in range(ntodo):            # ← 只执行这么多个
        handle = self._ready.popleft()
        handle._run()
```

### 快照机制的意义

`ntodo = len(self._ready)` 在执行前记录队列长度。执行期间如果有新回调被 `call_soon_threadsafe` 塞进来，**不会在本轮执行**，留到下一轮 `_run_once`。

### 回调和协程在同一个队列

`_ready` 队列里混着回调和协程的下一步，没有优先级区分，按入队顺序执行。协程 `await` 恢复时，它的下一步被包装成一个 handle 放进 `_ready`，和 `call_soon_threadsafe` 塞进来的回调没有任何区别。

### 一轮结束后的行为

做完一轮立刻开始下一轮，没有任何固定休眠。唯一会睡的情况是 `_ready` 队列空了，在 `epoll_wait` 里等 IO 或定时器到期。

"有活就干，没活就睡"——不是定时轮询，是事件驱动。

## 28. asyncio.sleep 的真正原理 — 不是阻塞，是注册定时器

### asyncio.sleep 做的事

```python
async def sleep(delay):
    future = loop.create_future()
    loop.call_later(delay, future.set_result, None)  # 往定时器堆里加一条记录
    await future   # 协程让出，return 回 _run_once
```

`call_later(0.1, future.set_result, None)` = 往 `_scheduled` 堆里插一条：0.1 秒后执行 `future.set_result(None)`。

### 执行流程

```
协程: await asyncio.sleep(0.1)
  → 注册定时器 + await future → 协程 return，事件循环继续转
  → _run_once 结束 → 下一轮 _run_once
  → epoll_wait(timeout=0.1s) → 超时返回
  → 检查定时器堆 → 0.1 秒到了 → future.set_result(None)
  → 协程的下一步进 _ready → 协程恢复
```

### asyncio.sleep vs time.sleep

| | 线程状态 | 协程状态 |
|---|---|---|
| `time.sleep(0.1)` | 冻住，什么都不能干 | 也冻住了（因为线程冻了） |
| `asyncio.sleep(0.1)` | 继续跑事件循环 | 只有这个协程暂停，别的照跑 |

`asyncio.sleep(1000)` 也不会阻塞其他协程。它只是往定时器堆里加了一条"1000 秒后 set_result"的记录，然后协程 return 了。事件循环继续正常转。

### 三种唤醒方式对比

| 类型 | 谁 set_result | 怎么唤醒事件循环 |
|------|-------------|----------------|
| `asyncio.sleep` | `_run_once` 自己（定时器到期） | epoll_wait 超时返回 |
| `to_thread` | 其他线程 | `call_soon_threadsafe` 写 self-pipe |
| socket IO | `_run_once` 自己（IO 就绪） | epoll_wait 收到 IO 事件返回 |

sleep 和 socket IO 都不需要外部唤醒。只有跨线程场景才需要 self-pipe。

## 29. watcher.py 中 call_soon_threadsafe 的实际应用

### 完整跨线程链路

```
watchdog Observer 线程（OS级文件监听）
  │
  │  文件变化 → on_any_event() 被调用
  │                │
  │                ▼
  │         _DebouncedNotifier.trigger()
  │                │
  │                ├─ lock 保护下: cancel 旧 Timer, 创建新 Timer
  │                │
  │                ▼
  │         threading.Timer 线程（等 0.3~0.5 秒防抖）
  │                │
  │                │  时间到 → _fire() 被调用
  │                │
  │                ▼
  │         loop.call_soon_threadsafe(flag.set)
  │                │
  │                │  写 self-pipe 唤醒 epoll_wait
  │                ▼
  │         事件循环线程醒来，执行 flag.set()
  │
  ▼
主循环 main.py 里 while True:
  await asyncio.to_thread(input, "> ")  ← 挂起，epoll_wait
  ...
  if flags.skills_changed.is_set():     ← 每轮检查 flag
      reload skills...
```

### 为什么只做 flag.set() 不做 reload

`flag.set()` 是纯内存操作，纳秒级完成，不会饿死任何协程。如果在回调里做 reload 文件之类的耗时操作，会冻住事件循环，阻塞所有协程。所以把真正的耗时工作留给主循环里的协程代码去做。

### 防抖机制

`_DebouncedNotifier` 用 `threading.Timer` + `threading.Lock` 实现防抖：
- 文件连续变化时，每次 trigger 都 cancel 旧 Timer、创建新 Timer
- Lock 保护 cancel + create 的原子性
- 只有最后一次变化后等待 delay 秒无新变化，才真正触发 `_fire()`

## 30. asyncio 核心模型总结

整个 asyncio 就两个核心概念：

- **`await`** 是切片点：协程在此处 return 回事件循环，控制权交还
- **`future`** 是恢复机制：某个地方 `set_result` 后，事件循环恢复等待的协程

所有异步操作底层都是 `await future`，区别只是谁来 `set_result`：

| 场景 | 底层 | 谁 set_result |
|------|------|--------------|
| `asyncio.sleep(1)` | `await future` | 定时器到期，event_loop 自己 |
| `await to_thread(fn)` | `await future` | 线程跑完，call_soon_threadsafe |
| `await sock.recv()` | `await future` | epoll_wait 收到数据，event_loop 自己 |

## 31. yield vs await — 两个完全不同的方向

`yield` 和 `await` 在 async generator 里共存，但干的事完全不同：

- **`yield value`** — 把 value 送出去给消费者（`async for` 的调用方）
- **`await something`** — 等回来一个结果给自己用

方向完全相反：
```
yield  = 快递员把包裹递给你（数据流向：函数 → 调用方）
await  = 你打电话叫外卖然后等送到（数据流向：被等待的东西 → 函数自己）
```

示例：
```python
async def middle():
    for val in inner():
        yield f"got:{val}"        # 把数据送给 outer 的 async for
        await asyncio.sleep(0)    # 等 sleep 完成，让出控制权给 event loop

async def outer():
    async for item in middle():
        print(item)
```

不能用 `await` 替代 `yield`：`await` 后面必须跟 awaitable 对象，语义是"等结果回来"。`yield` 的语义是"产出一个值，暂停自己，等消费者来取"。两者缺一不可。

## 32. yield 的去向 — 完全取决于谁在消费

`yield` 是个无脑传送带，不认识 Future，不认识 event loop。它只把东西递给"上一层调用 `next()` 的人"。

| 写法 | yield 的值去哪 | 谁消费 |
|---|---|---|
| `def f(): yield x` | `for val in f()` | 普通 for 循环 |
| `async def f(): yield x` | `async for val in f()` | async for 循环 |
| `Future.__await__` 里的 `yield self` | event loop 的 Task.__step | asyncio 内部机制 |

只有 `Future.__await__` 内部的 `yield self` 才能穿透到 event loop。这是 asyncio 框架自己写的内部机制。

## 33. Task.__step 源码 — yield 值的判定逻辑

`Task.__step_run_and_handle_result` 对 `coro.send(None)` 返回的 result 做判定：

```python
result = coro.send(None)   # yield 出来的值

# 1. 有 _asyncio_future_blocking 属性？→ 是 Future
blocking = getattr(result, '_asyncio_future_blocking', None)
if blocking is not None:
    if blocking:
        result.add_done_callback(self.__wakeup)  # 注册回调
        self._fut_waiter = result                # 等 Future 完成

# 2. result is None？→ 裸 yield，让出一轮
elif result is None:
    self._loop.call_soon(self.__step)   # 下一轮继续

# 3. 是 generator？→ 报错
elif inspect.isgenerator(result):
    RuntimeError('yield was used instead of yield from')

# 4. 其他任何东西？→ 报错
else:
    RuntimeError(f'Task got bad yield: {result!r}')
```

关键发现：Task.__step 只认两种合法的 yield 值：
- **Future 对象**（有 `_asyncio_future_blocking=True`）→ 注册回调等待
- **None**（裸 yield）→ 让出一轮，下轮继续

其他任何东西 yield 出来都会报 RuntimeError。

## 34. 普通 generator 进不了 Task

`Task.__init__` 有检查：
```python
if not coroutines.iscoroutine(coro):
    raise TypeError(f"a coroutine was expected, got {coro!r}")
```

只接受 coroutine（`async def` 产出的），不接受普通 `def` + `yield` 的 generator。

所以 `def my_gen(): yield some_future` 然后 `await my_gen()` 会直接 TypeError。

历史遗留：Python 3.4 时代有 `@asyncio.coroutine` 装饰器给普通 generator 打标记让 Task 认它，但已在 Python 3.12 移除。

整条穿透链路：
```
你写: await asyncio.sleep(1)
  → sleep 内部: await future
  → Future.__await__: yield self          ← 唯一合法的穿透点
  → Task.__step: result = coro.send(None) ← 接住 yield 出来的 Future
  → 检查 _asyncio_future_blocking → 注册回调 → 协程挂起
```
| `await task` | `await future` | 子协程跑完，自动 set_result |

事件循环本身就是一个 `while True: _run_once()` 循环，每轮检查定时器、epoll_wait、执行 `_ready` 队列。有活就干，没活就睡。不是定时轮询，是事件驱动。

## 35. __anext__ 作为隔离层 — async generator 如何同时服务两个方向

### 问题：async generator 里 yield 和 await 不会混淆吗？

async generator 同时做两件事：
- `yield value` — 产出值给消费者（async for）
- `await future` — 等待异步操作完成（event loop 调度）

两种 yield 走的是同一条 `send()`/`throw()` 通道，为什么不会混淆？

### 答案：async_generator_asend 是隔离层

`__anext__()` 返回的 `async_generator_asend` 对象负责分辨两种 yield：

1. 驱动 async generator 执行（调用内部 `send()`）
2. 如果 generator `yield value`（产出业务值）→ asend 截获，`raise StopIteration(value)`
3. 如果 generator 内部 `await future` 导致 yield future → asend 不认识，透明转发给上层

### 两种 yield 的命运

| generator 内部行为 | asend 看到什么 | asend 做什么 | 最终去向 |
|---|---|---|---|
| `yield 1`（业务值） | 收到值 1 | `raise StopIteration(1)` | 消费者拿到 `i=1` |
| `await future` → 内部 `yield self` | 收到 Future | 不认识，re-yield 给上层 | 穿透到 Task.__step → event loop |

### 工作流程示例

```python
async def gen():
    await asyncio.sleep(0)   # 内部 yield future → asend 透传 → event loop
    yield 42                 # asend 截获 → StopIteration(42) → 消费者拿到

async def consumer():
    async for val in gen():  # val = 42
        print(val)
```

执行过程：
```
consumer: await gen_asend.__next__()
  → asend 驱动 gen.send(None)
  → gen 执行到 await asyncio.sleep(0) → yield Future
  → asend 收到 Future，不认识 → re-yield Future
  → consumer 的 await 也收到 Future → 穿透到 Task.__step → event loop 挂起

  ... event loop 处理 sleep，恢复 ...

  → Task.__step send(None) 恢复 consumer
  → consumer 的 await 恢复 → asend 恢复 → gen.send(None) 恢复
  → gen 继续执行到 yield 42
  → asend 收到 42 → raise StopIteration(42)
  → consumer 的 await 完成 → val = 42
```

### 这就是为什么 async generator 能"混用" yield 和 await

asend 作为中间层，自动把两种 yield 分流到不同的消费者。generator 内部不需要知道"这个 yield 给谁"——asend 替它做了路由。

## 36. CPython 的真实协程实现 — 帧对象，不是状态机（对第 4、17-18、22 节的勘误）

### 编译型语言（Rust/C）的做法：编译为 switch-case 状态机

前面第 4、17-18、22 节描述的 switch-case + goto 模型是 **Rust 和 C 的实际实现方式**：
- 编译器在编译期把 async fn 拆成 enum 的不同 variant
- 每个 yield/await 点对应一个 variant
- 局部变量搬到 enum struct 的字段里
- 恢复执行 = match variant → 跳转到对应分支

### CPython 的做法：完全不同

CPython **不做任何编译期转换**。每个函数（包括 async def、generator）都有独立的 PyFrameObject：

```
PyFrameObject {
    f_lasti:       当前执行到的字节码偏移量（指令指针）
    f_localsplus:  局部变量数组
    f_stacktop:    求值栈顶指针
    f_code:        字节码对象（PyCodeObject）
}
```

### yield 的实现

`yield` 在 CPython 中的行为：
1. 保存当前 f_lasti（记住执行到哪条字节码）
2. 把 yield 的值放到返回位置
3. return 退出 C 层的 `_PyEval_EvalFrameDefault`（字节码解释器主循环）
4. frame 对象**不销毁**，留在堆上

### send() 的实现

`send(value)` 的行为：
1. 把 value 压入 frame 的求值栈
2. 重新进入 `_PyEval_EvalFrameDefault`
3. 从 f_lasti 位置**继续执行**后续字节码
4. 不需要 switch-case，直接跳到上次暂停的字节码偏移量

### 嵌套 generator/coroutine 不会合并

```python
async def outer():
    await inner()  # outer 和 inner 是两个独立的 frame

async def inner():
    await asyncio.sleep(1)
```

- outer 有自己的 PyFrameObject（f_lasti=X）
- inner 有自己的 PyFrameObject（f_lasti=Y）
- **不会**被编译成一个大 switch-case
- `yield from` / `await` 的实现：outer 的 frame 调用 inner 的 frame 的 `send()`，inner yield 后 outer 的 `YIELD_FROM` 字节码实现自动 re-yield

### 为什么前面的 switch-case 模型仍然有价值

- **概念上等价**：暂停-恢复的语义是一样的，只是实现手段不同
- **理解调度流程**：用 switch-case 思考"每次进入从哪里继续"是正确的心智模型
- **跨语言通用**：Rust、Go、C# 的协程确实是编译为状态机
- **性能差异**：Rust 的编译期状态机是零开销抽象；CPython 的 frame 方案有额外开销（frame 对象分配、字节码解释）

### 对比总结

| | Rust/C | CPython |
|---|---|---|
| 转换时机 | 编译期 | 无转换 |
| 暂停点表示 | enum variant / switch case | f_lasti 字节码偏移 |
| 局部变量存储 | struct 字段 | frame.f_localsplus |
| 嵌套协程 | 可能编译为单一状态机 | 独立 frame，运行时链接 |
| 恢复机制 | match/switch 跳转 | 字节码解释器从 f_lasti 继续 |

## 37. 完整的 async for 传播链 — a→b→c 三层嵌套详解

### 示例代码

```python
import asyncio

async def c():
    await asyncio.sleep(0)   # yield future → event loop
    yield 1                  # yield value → 消费者

async def b():
    async for i in c():      # await c_asend → 驱动 c
        yield i              # re-yield → 消费者

async def a():
    async for i in b():      # await b_asend → 驱动 b → 驱动 c
        print(i)             # 最终拿到值
```

### await 部分的传播（Future 穿透到 event loop）

当 c 内部执行 `await asyncio.sleep(0)` 时：

```
c: await sleep(0)
  → sleep 内部: await future → Future.__await__ → yield self（Future 对象）
  → c 的 frame 暂停，Future 作为 yield 值返回

c_asend: 驱动 c.send(None)，收到 Future
  → c_asend 不认识 Future（不是 generator 的 yield value）
  → re-yield Future 给上层

b: await c_asend，收到 Future
  → b 的 frame 暂停，Future 继续往上传

b_asend: ���动 b.send(None)，收到 Future
  → b_asend 也不认识 → re-yield Future

a: await b_asend，收到 Future
  → a 的 frame 暂停，Future 继续往上传

Task.__step: coro.send(None) 收到 Future
  → 检查 _asyncio_future_blocking=True → 注册 __wakeup 回调 → 协程挂起
  → event loop 自由调度其他协程
```

**关键：** Future 从最内层的 `Future.__await__` 一路穿透 c_asend → b → b_asend → a → Task.__step，每一层都因为"不认识"而 re-yield，最终到达 event loop。

### yield 部分的传播（值传递给消费者）

当 c 执行 `yield 1` 时：

```
c: yield 1

c_asend: 驱动 c，收到 1
  → c_asend 认出这是 generator 的 yield value
  → raise StopIteration(1)

b: await c_asend 完成（StopIteration.value = 1）
  → i = 1
  → b: yield 1（re-yield 给 b 的消费者）

b_asend: 驱动 b，收到 1
  → raise StopIteration(1)

a: await b_asend 完成（StopIteration.value = 1）
  → i = 1
  → print(1)
```

**关键：** 值在每一层的 asend 处被截获（`raise StopIteration(value)`），不会穿透到 event loop。每层 async for 拿到值后执行自己的循环体，如果有 `yield`，值重新进入下一层 asend 的截获流程。

### 关键观察

1. **同一条 send() 通道**传递两种完全不同的东西，靠 asend 在中间做路由
2. **Future 穿透**：asend 不认识 Future，所以 re-yield，一路穿透到 Task.__step
3. **值截获**：asend 认识 generator 的 yield value，所以 `raise StopIteration(value)`，不让它继续往上跑
4. 每增加一层 async for 嵌套，就多一个 asend 中间人，但行为完全一致——路由规则不变

## 38. Generator vs Coroutine — 同一机制，不同接口

### 底层共享：PyFrameObject + send()/throw()

Python 的 generator、coroutine、async generator 底层都使用完全相同的执行机制：
- 都有 PyFrameObject，都用 f_lasti 记录暂停位置
- 都通过 `send()` 恢复执行，`throw()` 注入异常
- 都是"暂停-恢复"的函数

### 区别只在 code object 的 flag

| 写法 | CO_* flag | 产出的对象类型 | 谁来驱动 |
|---|---|---|---|
| `def f(): yield x` | CO_GENERATOR | generator | for 循环 / 手动 next() |
| `async def f(): return x` | CO_COROUTINE | coroutine | Task 包装，event loop 驱动 |
| `async def f(): yield x` | CO_ASYNC_GENERATOR | async_generator | `__anext__()` / async for |

### flag 决定了什么

- **CO_GENERATOR**：调用 `f()` 返回 generator object，可以 `next()`/`send()`/`throw()`
- **CO_COROUTINE**：调用 `f()` 返回 coroutine object，必须被 `await` 或 Task 包装
- **CO_ASYNC_GENERATOR**：调用 `f()` 返回 async_generator object，必须用 async for 或 `__anext__()`

### 三者的"暂停"和"恢复"是同一个操作

```
暂停：保存 f_lasti → return 退出 _PyEval_EvalFrameDefault → frame 留在堆上
恢复：进入 _PyEval_EvalFrameDefault → 从 f_lasti 继续执行字节码
```

不管是 generator 的 `yield`、coroutine 的 `await`、还是 async generator 的 `yield`/`await`，底层都是同一段 C 代码在处理。

### 本质

`async` 关键字不改变函数的执行机制，只改变它的**接口约定**：
- generator：调用者直接 `next()` 驱动，拿 yield 的值
- coroutine：交给 event loop，通过 Future + Task.__step 驱动
- async generator：通过 asend 驱动，同时支持 yield 值和 await 异步操作（见第 35 节）

三者底层完全共享 PyFrameObject + f_lasti + send()/throw()。区别只是上层的"驾驶员"不同。

## 39. 回调与协程在 _run_once 中的平级关系

### 回调和 Task.__step 是同一个队列里的 handle

`_ready` 队列里混着回调和协程的下一步，没有类型区分，按入队顺序执行：

```
_ready 队列: [Task_A.__step, flag.set, Task_B.__step]

执行顺序：
1. Task_A.__step → 协程 A 跑到 await future → yield self → __step return
2. flag.set()    → 设置 flag → return
3. Task_B.__step → 协程 B 跑到 await future → yield self → __step return
4. 本轮 ntodo=3 执行完毕
```

**协程 await 不是"给回调让路"**，是"协程的 __step 执行完了，_run_once 的 for 循环继续下一个 handle"。回调和 __step 是平级的。

### 不 await 的协程会霸占 __step

如果协程内部没有 await（纯同步代码），`coro.send(None)` 永远不 return，`__step` 永远不结束：

```python
async def bad():
    while True:
        do_heavy_work()   # 没有 await → send() 永远不 return
```

结果：`_run_once` 的 for 循环卡在第一个 handle 上，后面的回调和其他协程的 __step 全部饿死。

### GIL 保证线程安全，不需要显式锁

`call_soon_threadsafe` 从其他线程往 `_ready` 塞 handle 时，不需要加锁：

```python
# 线程 A（watchdog 线程）：
self._ready.append(handle)     # GIL 保证原子操作

# 线程 B（event loop 线程）：
handle = self._ready.popleft() # GIL 保证原子操作
```

CPython 的 GIL 保证同一时刻只有一个线程执行 Python 字节码，`deque.append` 和 `deque.popleft` 不会交叉执行。

### 快照机制保证执行稳定性

```python
ntodo = len(self._ready)          # 快照：比如 ntodo=5
for i in range(ntodo):            # 只执行 5 个
    handle = self._ready.popleft()
    handle._run()
    # 执行期间如果其他线程 append 了新 handle
    # deque 变长了，但 for 循环只跑 ntodo=5 次
    # 新 handle 留在 deque 尾部，下一轮处理
```

- 执行不影响添加：其他线程随时可以 append
- 添加不影响执行：ntodo 已经拍了快照，新加的不在本轮范围
- 不需要锁：GIL + 快照 = 天然线程安全

## 40. 异步 I/O 三种模式对比

### 模式 1：纯异步（epoll + 非阻塞 fd）

socket 读写、subprocess pipe、asyncio.sleep 都属于这种模式：
- fd 注册到 epoll（或定时器注册到 _scheduled 堆）
- 协程 await future → 暂停
- IO 就绪 / 定时器到期 → 回调 → future.set_result → 协程恢复
- **全程无额外线程**

### 模式 2：线程桥接（call_soon_threadsafe）

to_thread、watchdog 文件监听属于这种模式：
- 阻塞操作在工作线程里执行
- 完成后通过 `call_soon_threadsafe` 把回调塞进 _ready + 写 self-pipe 唤醒 epoll_wait
- **需要额外线程**

### 模式 3：定时器

asyncio.sleep 的特殊情况：
- `call_later(delay, future.set_result, None)` 往 _scheduled 堆里插记录
- epoll_wait 的 timeout 设为下一个定时器到期时间
- 超时返回 → 检查堆 → 到期的移入 _ready → 执行回调 → set_result
- **无额外线程，无 fd**

### 统一模型

不管哪种模式，最终都归结为：

```
某个地方调用 future.set_result(value)
  → future 的 done_callback 触发
  → Task.__step 进入 _ready 队列
  → _run_once 执行 __step
  → 协程恢复，拿到 value
```

| 场景 | 等什么 | 谁通知 event loop | 谁调 set_result | 有额外线程吗 |
|---|---|---|---|---|
| `asyncio.sleep(1)` | 时间到 | epoll_wait 超时 | _run_once 自己（定时器回调） | 无 |
| `await reader.read()` | socket 数据到达 | epoll_wait 返回 fd 可读 | _run_once 自己（IO 回调） | 无 |
| `await proc.communicate()` | 子进程输出+退出 | epoll_wait 返回 pipe 可读 | _run_once 自己（IO 回调） | 无 |
| `await to_thread(input)` | 线程阻塞调用完成 | call_soon_threadsafe + self-pipe | 工作线程（桥接到 event loop） | 有 |
| watcher 文件监听 | OS 文件变化 | call_soon_threadsafe + self-pipe | watchdog 线程（桥接到 event loop） | 有 |

## 41. Socket 异步 I/O 完整链路 — Transport/Protocol/StreamReader 三层架构

### 你写的代码

```python
async def fetch():
    reader, writer = await asyncio.open_connection('example.com', 80)
    writer.write(b'GET / HTTP/1.1\r\nHost: example.com\r\n\r\n')
    data = await reader.read(4096)
    return data
```

### 三层架构

```
你的代码层:     async def fetch() → await reader.read()
                     ↕ Future 连接
Protocol 层:    StreamReaderProtocol → feed_data → set_result
                     ↕ 方法调用
Transport 层:   _SelectorSocketTransport → register fd → _read_ready
                     ↕ epoll
OS 内核层:      epoll_wait → fd 可读事件
```

### Transport 层 — 持有 fd，负责底层 IO

```python
class _SelectorSocketTransport:
    def __init__(self, loop, sock, protocol):
        self._sock = sock
        self._protocol = protocol
        # 关键：创建时就注册 fd 到 epoll（一次性）
        self._loop._selector.register(sock.fileno(), EVENT_READ, self._read_ready)

    def _read_ready(self):
        """epoll_wait 返回时被调用"""
        data = self._sock.recv(65536)        # 非阻塞读，数据已就绪，瞬间完成
        self._protocol.data_received(data)   # 把数据交给 Protocol
```

**fd 注册发生在 Transport 创建时（连接建立时），不是每次 read() 时。** `_read_ready` 是注册给 epoll 的回调，不是你的协程。

### Protocol 层 — 连接 Transport 和 StreamReader

```python
class StreamReaderProtocol:
    def data_received(self, data):
        self._stream_reader.feed_data(data)  # 把数据喂给 StreamReader
```

### StreamReader 层 — 管理缓冲区和 Future

```python
class StreamReader:
    def __init__(self):
        self._buffer = bytearray()
        self._waiter = None              # Future

    async def read(self, n):
        if self._buffer:
            # 缓冲区有数据，直接返回，不等
            data = bytes(self._buffer[:n])
            del self._buffer[:n]
            return data
        # 缓冲区空，创建 Future 等数据
        self._waiter = self._loop.create_future()
        await self._waiter               # 协程暂停
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data

    def feed_data(self, data):
        self._buffer.extend(data)        # 数据写入缓冲区
        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            waiter.set_result(None)      # 唤醒协程
```

### fd 注册与 Future 创建是两个独立时间点

- **fd 注册**：连接建立时，一次性。fd 一直在 epoll 里，只要有数据到就触发 `_read_ready`
- **Future 创建**：每次 `read()` 且缓冲区空时。如果缓冲区有数据，不创建 Future，直接返回
- `_read_ready` 里检查 `if waiter is not None`：可能数据到了但没人在 `await read()`，数据只存进缓冲区

### 三轮 _run_once 完成一次异步读取

```
_run_once 第 N 轮:
  [__step] 协程跑到 await reader.read()
    → 缓冲区空 → 创建 Future → await future → yield self → __step 结束
    → 同时：socket() + connect() + register(fd) 等同步操作在 await 之前一口气完成

_run_once 第 N+1 轮:
  epoll_wait → 数据到了 → _ready.append(_read_ready)
  [_read_ready] sock.recv() → protocol.data_received() → reader.feed_data()
    → buffer.extend(data) → future.set_result(None)
    → call_soon(__step)  ← 塞进 _ready 尾部，不在本轮快照里

_run_once 第 N+2 轮:
  [__step] 协程恢复 → 从 buffer 取数据 → data = "响应内容" → 继续跑
```

### 关键：你的协程和 asyncio 的内部回调是两个独立的东西

| 东西 | 是什么 | 谁创建的 | 在 _ready 里吗 |
|---|---|---|---|
| 你的协程 | Task 包装的 coroutine | 你写的 async def | 只有 __step 被 call_soon 时才在 |
| _read_ready | 普通回调函数 | asyncio 内部（Transport） | epoll_wait 返回后被加入 |
| Future | 连接两者的桥梁 | asyncio 内部（StreamReader） | 不在 _ready 里，它是数据对象 |

Future 和 fd 回调之间的连接不是通过 event loop，是通过 Python 对象引用链：
```
StreamReader 持有 _waiter（Future）
StreamReaderProtocol 持有 StreamReader
Transport 持有 Protocol
epoll 持有 Transport._read_ready
```

event loop 只负责"epoll_wait 返回后执行 _read_ready"，剩下的全是对象之间的方法调用。

## 42. Subprocess 异步 I/O

### create_subprocess_exec 的底层

```python
proc = await asyncio.create_subprocess_exec(
    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
)
```

内部步骤：
1. 创建子进程：Linux 用 `fork()` + `exec()`，Windows 用 `CreateProcess`（同步，但很快）
2. 创建 pipe fd 用于 stdout/stderr
3. pipe fd 设为非阻塞
4. pipe fd 注册到 epoll
5. 返回 Process 对象

### communicate() 等待输出

```python
stdout, stderr = await proc.communicate()
```

内部：
1. 创建 Future
2. 注册 stdout pipe fd 到 epoll（等可读）
3. 注册 stderr pipe fd 到 epoll（等可读）
4. `await future` → 协程暂停
5. 子进程往 stdout 写数据 → pipe 可读 → epoll_wait 返回 → 回调读取数据
6. 子进程退出 → SIGCHLD 信号 → event loop 收到 → `future.set_result((stdout, stderr))`
7. 协程恢复

### 与 socket 模式的对比

| | Socket | Subprocess |
|---|---|---|
| fd 来源 | socket() 创建 | pipe() 创建 |
| 数据方向 | 网络 ↔ 进程 | 子进程 → 父进程 |
| 完成信号 | 对端关闭连接 | SIGCHLD（子进程退出） |
| 注册到 epoll | 是 | 是 |
| 需要额外线程 | 否 | 否 |

底层模式完全一致：非阻塞 fd + epoll + Future + set_result。

## 43. create_task vs await future — 两种进入 _ready 的方式

### create_task：立刻入队

```python
task = asyncio.create_task(some_coro())
```

内部：
```python
class Task:
    def __init__(self, coro, loop):
        self._coro = coro
        self._loop = loop
        self._loop.call_soon(self.__step)   # 立刻塞进 _ready
```

不需要等任何 IO 或 Future。协程的第一次执行机会就是下一轮 _run_once。

### await future：等 set_result 后入队

```python
result = await some_future
```

内部：
```
协程跑到 await future → yield self → __step 结束
  → 协程挂起，不在 _ready 里
  → 等某个地方调 future.set_result()
  → set_result 触发 done_callback → call_soon(__step)
  → __step 进入 _ready → 下一轮 _run_once 执行 → 协程恢复
```

### 对比

| | create_task | await future |
|---|---|---|
| 什么时候进 _ready | 立刻（call_soon） | set_result 之后（call_soon） |
| 协程状态 | 还没开始跑 | 已经跑到 await 处暂停了 |
| 谁触发入队 | create_task 自己 | 外部 set_result |

两者之后的循环完全一致：__step 执行 → 协程跑到下一个 yield → 暂停 → 等 set_result → 恢复。区别只是"第一次进入 _ready"的方式不同。
