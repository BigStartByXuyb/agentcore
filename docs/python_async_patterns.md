# Python 异步编程模式总结

## 一、Coroutine 的本质

Python 中 `async def` 定义的函数调用后不会立即执行，而是返回一个 coroutine 对象（类似"待执行的包裹"）。必须通过 `await`、`create_task` 或 `gather` 才能真正运行。

```python
coro = some_async_func()   # 只创建 coroutine 对象，函数体一行都没跑
result = await coro         # 这时候才进入函数体，开始执行
```

---

## 二、三种异步调度模式

### 1. `await` — 阻塞等结果（串行）

```python
result = await some_async()  # 阻塞当前协程，等它完成才继续
```

- 适用：需要结果才能继续的场景
- 特点：当前协程会暂停，直到被 await 的协程完成

### 2. `asyncio.create_task()` — 后台跑（非阻塞）

```python
task = asyncio.create_task(some_async())  # 立即开始调度，不等
# ... 干别的事 ...
if task.done():                           # 检查是否完成（不阻塞）
    result = task.result()                # 完成了才能取值，否则报错
```

- 适用：不想等结果、后台执行的任务（如记忆检索、fire-and-forget）
- 特点：立即提交到事件循环开始调度

### 3. `asyncio.gather()` — 批量并发（等全部完成）

```python
results = await asyncio.gather(
    task_a(),
    task_b(),
    task_c(),
)
# results = [result_a, result_b, result_c]  按传入顺序对应
```

- 适用：多个独立任务需要并发执行，且需要全部结果
- 特点：内部对每个 coroutine 调 `create_task`，传进去就开始跑

---

## 三、Task 取值方式对比

| 方法 | 行为 | 是否阻塞 |
|---|---|---|
| `await task` | 等完成，拿返回值 | 阻塞（已完成则立即返回） |
| `task.result()` | 拿返回值（必须已完成） | 不阻塞（未完成则报错） |
| `task.done()` | 检查是否完成 | 不阻塞（返回 bool） |

```python
task = asyncio.create_task(some_async())

# 方式 A：非阻塞检查
if task.done():
    value = task.result()

# 方式 B：阻塞等待
value = await task
```

---

## 四、`gather` 与 `*` 解包

`gather` 接收多个独立参数，不是一个列表，所以需要 `*` 解包：

```python
tasks = [func(a), func(b), func(c)]

asyncio.gather(tasks)    # ❌ 传了一个列表
asyncio.gather(*tasks)   # ✅ 等价于 gather(func(a), func(b), func(c))
```

常见写法：列表推导 + 解包

```python
gathered = await asyncio.gather(
    *[_drain(c) for c in group.tool_call]
)
```

---

## 五、`yield` 与 `return` 的互斥性

Python 规则：函数体内有 `yield` → 变成 generator，`return` 不能带值。

```python
# 普通 async 函数 — 可以 return
async def foo() -> int:
    return 42  # ✅

# 有 yield — async generator，return 不能带值
async def bar() -> AsyncIterator[str]:
    yield "hello"
    return 42  # ❌ SyntaxError
```

### AsyncGenWithResult 包装器

解决"既要 yield 事件，又要返回最终结果"的需求：

```python
class AsyncGenWithResult(Generic[E, R]):
    """用 set_result() 代替 return，绕过语言限制"""

    async def events(self) -> AsyncIterator[E]: ...
    def set_result(self, value: R) -> None: ...
    @property
    def result(self) -> R: ...
```

使用方式：

```python
def some_operation() -> AsyncGenWithResult[AgentEvent, ResultType]:
    async def _impl(run: AsyncGenWithResult) -> AsyncIterator[AgentEvent]:
        yield ToolStart(...)          # 可以 yield 事件
        result = await executor()
        yield ToolEnd(...)            # 继续 yield
        run.set_result(result)        # 用 set_result 代替 return

    return AsyncGenWithResult(_impl)

# 调用方
run = some_operation()
async for ev in run.events():   # 消费事件
    handle(ev)
final = run.result              # 取最终结果
```

---

## 六、逐个 await vs gather 的区别

```python
# 串行 — 总耗时 = 1s + 1s + 1s = 3s
result_a = await task_a()   # 等1秒
result_b = await task_b()   # 再等1秒
result_c = await task_c()   # 再等1秒

# 并发 — 总耗时 = max(1s, 1s, 1s) = 1s
results = await asyncio.gather(task_a(), task_b(), task_c())
```

手动实现并发（等价于 gather）：

```python
running = [asyncio.create_task(t) for t in tasks]  # 全部立即开始
results = [await t for t in running]                # 逐个收结果，但已经在并发了
```

关键：`create_task` 提交到事件循环立即调度，之后的 `await` 只是收结果。

---

## 七、实际应用场景

### 场景 1：记忆检索（create_task — 不阻塞主流程）

```python
# 启动记忆检索，不等它
memory_task = asyncio.create_task(_prepare_memory_context(user_input))

# 直接开始 LLM 调用
response = await call_llm(...)

# 下一轮循环时检查记忆是否好了
if memory_task is not None and memory_task.done():
    memory_result = memory_task.result()
    if memory_result:
        history.inject_messages([memory_result])
    memory_task = None
```

### 场景 2：read-only 工具并发（gather — 等全部完成）

```python
async def _drain(call):
    r = run_tool_use(call.name, call.input, context)
    events = []
    async for ev in r.events():
        events.append(ev)
    return r.result, events

gathered = await asyncio.gather(*[_drain(c) for c in group.tool_call])
```

### 场景 3：后台记忆提取（create_task — fire-and-forget）

```python
asyncio.create_task(_run_extraction())  # 不取值，跑完就行
```
