# MCP Event Loop + 网络协议深度探索笔记

## 1. 为什么 MCP 需要独立线程 + 独立 Event Loop

### 设计意图：隔离
MCP 连接和主 agent loop 是完全不同的生命周期：
- **主 event loop**：随用户输入启停，管 LLM 流式响应、工具执行
- **MCP event loop**：进程启动到退出一直活着，管长连接维持、心跳、重连

独立 loop = 故障隔离 + 生命周期解耦。一个卡住的 MCP server 不会影响 LLM 流式响应。

### 技术必要性：避免死锁
当前架构中 tool executor 是同步函数（`def _executor`），内部用 `fut.result()` 阻塞等待。
如果 MCP loop 在主线程，`fut.result()` 阻塞主线程 = 阻塞 loop = 协程永远不执行 = 死锁。

### 架构图
```
主线程                              MCP 线程
┌─────────────────────┐            ┌─────────────────────┐
│ asyncio.run()       │            │ loop.run_forever()   │
│ ┌─ event loop ────┐ │            │ ┌─ event loop ────┐ │
│ │ agent_loop      │ │            │ │ lifecycle_1     │ │
│ │ LLM stream      │ │  提交协程   │ │ lifecycle_2     │ │
│ │ tool executor   │ ──────────→  │ │ call_tool       │ │
│ │ user input      │ │            │ │ list_tools      │ │
│ └─────────────────┘ │            │ └─────────────────┘ │
└─────────────────────┘            └─────────────────────┘
```

### 关键代码
```python
_mcp_loop = asyncio.new_event_loop()
_mcp_thread = threading.Thread(target=_mcp_loop.run_forever, daemon=True)
_mcp_thread.start()
```
- `new_event_loop()`：创建全新的独立 event loop
- `run_forever()`：让 loop 在后台线程永久运行，等待任务
- `daemon=True`：主进程退出时自动销毁

---

## 2. 线程中的 Event Loop 与协程

**每个线程可以运行自己的 event loop，每个 event loop 独立调度自己的协程。**

协程切换是 event loop 内部的事，不跨线程。两个 loop 之间唯一的桥梁是 `run_coroutine_threadsafe()`。

---

## 3. 三种任务提交方式对比

### create_task
```python
task = asyncio.create_task(some_coro())
result = await task
```
- 注册到**当前线程**正在跑的 event loop
- 返回 `asyncio.Task`（可以 await）
- 场景：在 async 函数里启动并发协程

### run_coroutine_threadsafe
```python
fut = asyncio.run_coroutine_threadsafe(some_coro(), target_loop)
result = fut.result(timeout=30)
```
- 注册到**指定的** event loop（通常在另一个线程）
- 返回 `concurrent.futures.Future`（不能直接 await，可以 `fut.result()` 阻塞等）
- 如需 await，可用 `asyncio.wrap_future(fut)` 桥接
- 场景：同步函数跨线程调异步代码

### call_soon_threadsafe
```python
loop.call_soon_threadsafe(callback, arg1, arg2)
```
- 注册到**指定的** event loop
- 返回 None（fire-and-forget）
- 场景：跨线程触发一个简单操作，不需要返回值

### 共同点
- **都是一次性的**，执行完就从队列移除
- 本质都是往 event loop 的就绪队列里塞东西

### 关键区别
- `create_task` 给协程用（能 await）
- 另外两个给同步函数/跨线程用（调用者不是协程，没有 await 能力）
- **能不能 await 取决于调用者是不是协程，不取决于目标协程在哪个 loop 跑**

---

## 4. Event Loop 的运行机制

### run_forever() 不会停
```python
def run_forever(self):
    while True:
        if 就绪队列有回调: 执行回调们
        if 有 I/O 事件: 处理 I/O
        if 啥都没有: 阻塞等待（epoll/select/IOCP 挂起，不是 busy loop）
```
空闲时 loop 是**休眠**的，只有新任务投递或 I/O 就绪时才会醒来。

### Task vs 队列回调
- **队列回调**：一次性，执行完移除
- **Task**：由回调创建出来的，独立于队列存在。Task 的生命周期由协程决定（协程 return 了 Task 才结束）

`_async_lifecycle` 的 Task 一直活着，是因为协程 `await _close_event.wait()` 没结束，不是队列在反复执行它。

---

## 5. asyncio.Event vs I/O Future

### I/O Future（操作系统参与）
```
await socket.read()
  → loop 注册 fd 到 epoll/IOCP
  → 协程暂停
  → 操作系统：数据来了！→ loop set future → 协程恢复
```
触发者：**操作系统**

### asyncio.Event.wait()（纯 Python）
```python
self._close_event = asyncio.Event()
await self._close_event.wait()
```
内部原理：Event 创建一个 future，协程 await 它。`event.set()` 时内部 `fut.set_result()` 解开等待。

触发者：**你自己的代码**（如 `close()` 调用 `call_soon_threadsafe(event.set)`）

**暂停/恢复机制一样（await future → set future），区别只在于谁触发 set。**

---

## 6. MCP 连接的 AsyncExitStack

### 为什么用 AsyncExitStack
管理两层异步资源，保证异常时也能正确清理：
```python
async with AsyncExitStack() as stack:
    read, write, _ = await stack.enter_async_context(
        streamablehttp_client(url=self._cfg.url)    # 第 1 层：传输通道
    )
    session = await stack.enter_async_context(
        ClientSession(read, write)                   # 第 2 层：MCP 协议会话
    )
```

- `enter_async_context` 是工具函数，把任何 `async with` 对象注册到 stack
- 退出时按**后进先出**顺序清理：先关 Session（协议层）→ 再关 HTTP（传输层）
- 不管哪一步异常，已 enter 的资源都会正确关闭

### read/write 的本质

`streamablehttp_client` 返回的 `read`/`write` 是 SDK 提供的**抽象统一通道**，不直接对应单个 HTTP 连接：

```
write(message) → SDK 内部发一个 POST 请求（发送 JSON-RPC 消息）
                 POST 的 SSE 响应被 SDK 内部读取后塞进 read 通道
                 write 本身不返回响应

read()         → 从统一通道读取下一条消息（接收 JSON-RPC 消息）
                 消息来源可能是：
                 1. POST 的 SSE 响应（工具调用结果）— SDK 内部读取后转入
                 2. GET 长连接的 SSE 推送（服务端主动通知）— SDK 内部读取后转入
```

SDK 内部架构：
```
                    ┌─── POST /mcp ──→ 服务端
write(msg) ────→   SDK   ←── SSE response ──┐
                    │                        ↓
                    │                   统一 read 通道 ──→ read()
                    │                        ↑
                    └─── GET /mcp (可选) ─────┘
                         (服务端主动推送)
```

HTTP 层面：POST 响应确实在同一个 HTTP response body 里以 SSE 格式返回（跟普通 HTTP 请求-响应一样，只是 response 是流式的）。但 SDK 把这个细节藏起来，对外只暴露统一的 read/write 通道。

---

## 7. HTTP、SSE、TCP 的关系

### 分层架构
```
你的代码：   业务逻辑（调工具、发请求）
HTTP：      请求格式规范（请求头、状态码、Content-Type）
TCP：       可靠传输（拆包、编号、确认、重传、排序）
IP：        寻址路由（从 A 机器到 B 机器）
```

### TCP 是底层管道
- HTTP 跑在 TCP 上面，不是替代关系
- TCP 是字节流，没有消息边界
- HTTP 在 TCP 上加了格式规范（请求头、Content-Length 等），解决"怎么分割消息"的问题

### TCP 序列号（Sequence Number）— 排序的依据
每个 TCP 包都带一个序列号，表示"这个包的数据从整个流的第几个字节开始"：
```
发送 "helloworld"（10 ��节）：
  包1: [seq=1, len=5]  hello    ← 从第 1 字节开始
  包2: [seq=6, len=3]  wor      ← 从第 6 字节开始
  包3: [seq=9, len=2]  ld       ← 从第 9 字节开始

接收方不管到达顺序，按 seq 排序拼接即可：
  到达顺序：包3 → 包1 → 包2（乱序）
  按 seq 排：hello + wor + ld = helloworld
```
seq 是字节编号（不是��编号），接收方知道每个字节在原始数据中的精确位置。

### TCP 做了什么
- **丢包重传**：每个包编号 + ACK 确认 + 超时重传，上层看不到丢包
- **有序保证**：接收方按编号排序，有空洞时不交给上层，凑齐连续部分才往上交
- **保证**：要么收到完整有序的数据，要么报错（ConnectionError），不存在悄悄丢一段

### TCP 不做什么
- **不管重连**：连接断了只报错，重连是应用层（你的代码）的事
- **不管粘包**：TCP 是字节流，多次 send 的数据可能粘在一起或从中间断开

### HTTP 解决粘包
```
方式 1：Content-Length: 15 → 读够 15 字节 = 一条完整消息
方式 2：Transfer-Encoding: chunked → 每块前面有长度，0 = 结束
SSE：   用 \n\n（两个换行）分隔每条消息
```

### TCP 流式传输的含义
不是等全部数据到齐才交给上层，是**连续的数据到了就交**：
```
包1 到了 → 马上交给上层
包2 丢了 → 卡住等重传（包3 虽然到了但不连续，先存着）
包2 补上 → 包2+包3+后续 一起交给上层
```
数据像水流一样，到一点给一点，只在有空洞时暂时堵住。

---

## 8. SSE vs 普通 HTTP

### 普通 HTTP（一问一答）
```
客户端 POST /api → 服务端返回响应 → 结束
服务端不能主动联系客户端（每次交互完就"失联"了）
```

### SSE（服务端推送）
```
客户端 GET /mcp → 服务端不关闭响应 → 持续往响应体里写数据
服务端可以主动推消息，不需要客户端再问
```

本质：**SSE 就是服务端的 HTTP 响应一直不结束**，持续往里追加数据。

### MCP Streamable HTTP 的传输模型

**HTTP 层面（两条独立的 HTTP 通道）：**
```
POST /mcp → 客户端发 JSON-RPC 请求 → 服务端以 SSE 格式在同一个 response 里流式返回结果
            跟普通 HTTP 一样：请求-响应在同一个连接上完成，response 结束后请求就结束
            区别只是 response body 是 SSE 格式（text/event-stream），可以逐条推送事件
            对于简单请求，可能只有一个 event 就结束了，效果和普通 JSON 响应一样

GET  /mcp → SSE 长连接（可选），专门接收服务端主动推送的通知（如 tools/list_changed）
            这条连接不负责接收 POST 的响应
```

**SDK 层面（统一抽象）：**
```
SDK 内部分别管理 POST 响应和 GET 推送，但把两者汇入同一个 read 通道
上层代码只看到 write(msg) 发送、read() 接收，不需要关心消息来自哪条 HTTP 连接
ClientSession 内部有 read loop 统一处理 read 通道的所有消息
```

### HTTP 版本与连接复用
- HTTP/1.0：每次请求完关闭 TCP 连接（真的丢）
- HTTP/1.1：默认 keep-alive，TCP 连接复用（不丢，但请求还是一问一答）
- keep-alive 和 SSE 的区别：前者是复用管道减少握手开销，后者是让服务端能主动推数据

---

## 9. 域名与 IP

域名只是 IP 的别名：
```
http://example.com/api
  → DNS 查询：example.com → 93.184.216.34
  → TCP 连接：93.184.216.34:443
  → HTTP 请求头：Host: example.com
```
TCP 只认 IP + 端口。域名的作用：给人看（好记）+ 让一台服务器区分不同网站（通过 Host 头）。

---

## 10. 服务端流式输出

服务端数据不一定是一次性完整的：
```python
# LLM 流式响应 — 数据逐步产生，产生一点发一点
async def stream_response(request):
    for token in llm.generate():
        await response.write(f"data: {token}\n\n")
```
不是手动拆包，是**数据本来就是一点一点产生的**。HTTP 的 `Transfer-Encoding: chunked` 和 SSE 格式都支持这种逐步发送的模式。

---

## 11. Claude Code 的 MCP 工具变更通知（当前项目未实现）

Claude Code 在 `useManageMCPConnections.ts` 中注册了 notification handler：
```javascript
if (client.capabilities?.tools?.listChanged) {
    client.client.setNotificationHandler(
        ToolListChangedNotificationSchema,
        async () => {
            const newTools = await fetchToolsForClient(client)
            updateServer({ ...client, tools: newTools })
        },
    )
}
```

监听三种变化：tools/list_changed、prompts/list_changed、resources/list_changed。

当前项目（my-agent）只在启动时调用一次 `register_mcp_tools`，运行中服务端工具变化不会被感知。这是后续可以补的功能。

---

## 12. session.call_tool 的唤醒链路（源码验证）

源码位置：
- `mcp/shared/session.py` — BaseSession（send_request、_receive_loop、_handle_response）
- `mcp/client/streamable_http.py` — StreamableHTTPTransport（POST/GET → 统一 read 通道）

MCP SDK 的 `ClientSession.call_tool` 是 `async def`（异步）。调用链路：

```
call_tool / list_tools 内部调 send_request (session.py:240)：

1. 创建 per-request 的 memory object stream 并按 id 注册：
   response_stream, response_stream_reader = anyio.create_memory_object_stream(1)
   self._response_streams[request_id] = response_stream   # ← 按 id 注册发送端

2. 通过 write_stream 发送请求：
   await self._write_stream.send(SessionMessage(message=jsonrpc_request))
   → 传输层的 post_writer 收到 → 内部发 POST 到服务端

3. 等待自己专属的 response stream：
   response_or_error = await response_stream_reader.receive()  ← 协程暂停

4. BaseSession 内部的 _receive_loop（另一个 Task，__aenter__ 时启动）：
   async def _receive_loop(self):
       async for message in self._read_stream:   # ← 从统一 read 通道读取
           if isinstance(message, JSONRPCRequest):
               # 服务端发来的请求（如 sampling）→ 分发给 handler
           elif isinstance(message, JSONRPCNotification):
               # 通知（如 tools/list_changed, progress）→ 分发给 handler
           else:  # Response or error
               await self._handle_response(message)

5. _handle_response 按 id 路由 (session.py:479)：
   response_id = self._normalize_request_id(root.id)
   stream = self._response_streams.pop(response_id)  # ← 按 id 找到对应的发送端
   await stream.send(root)                            # ← 塞结果进去，唤醒步骤3的 await

6. response_stream_reader.receive() 解开 → send_request 拿到结果 → 返回
```

### 关键细节：不是 Future，是 per-request memory stream

之前笔记里写的 `pending_requests[id] = future` + `set_result` 是简化模型。
实际 SDK 用的是 **anyio.MemoryObjectStream**（容量为 1 的内存通道）：

```
简化模型（概念正确，实现不同）：
  pending_requests[42] = future
  await future
  ...
  pending_requests[42].set_result(result)

实际源码：
  response_streams[42] = MemoryObjectSendStream       ← 注册发送端
  await response_stream_reader.receive()               ← 在接收端等
  ...
  stream = response_streams.pop(42)                    ← 按 id 找到发送端
  await stream.send(root)                              ← 往里塞，唤醒 receive()
```

效果一样（按 id 匹配 + 唤醒等待者），但用 stream 而不是 Future，
因为 SDK 基于 anyio（不是 asyncio），anyio 推荐用 memory stream 做协程间通信。

### read 通道的数据来源（传输层）

_receive_loop 里 `async for message in self._read_stream` 读取的是统一 read 通道。
传输层（streamable_http.py）负责把两条 HTTP 通道的数据汇入这一个 read 通道：

```python
# streamable_http_client 创建统一通道 (streamable_http.py:634)
read_stream_writer, read_stream = anyio.create_memory_object_stream(0)

# POST 响应 → 统一通道 (_handle_post_request → _handle_sse_event/handle_json_response)
#   解析 POST 的 SSE/JSON 响应 → await read_stream_writer.send(session_message)

# GET 推送 → 统一通道 (handle_get_stream → _handle_sse_event)
#   解析 GET 长连接的 SSE 推送 → await read_stream_writer.send(session_message)
```

### HTTP 层面 vs SDK 层面

```
HTTP 层面（streamable_http.py 管理）：
  POST：客户端发 JSON-RPC → 服务端在同一个 response 里返回结果（SSE 或 JSON 格式）
        跟普通 HTTP 一样，请求-响应在同一个连接上完成
  GET： SSE 长连接（可选），接收服务端主动推送的通知
  两条独立的 HTTP 通道

SDK 层面（session.py 管理）：
  传输层把两条通道的消息汇入一个 read_stream
  _receive_loop 统一从 read_stream 读取所有消息
  按消息类型分发：Response → 按 id 路由到对应的 response_stream
                  Request → 分发给 handler
                  Notification → 分发给 handler
```

### 运行时的 Task 分布

MCP event loop 里同时跑着多个 Task：
- **Task A**：业务协程（send_request），发请求后 await response_stream_reader.receive()
- **Task B**：_receive_loop，从统一 read_stream 读消息，按类型分发
- **Task C**：post_writer，从 write_stream 读取待发送的消息，发 POST，读 POST 响应 → 塞进 read_stream
- **Task D（可选）**：handle_get_stream，读 GET 长连接推送 → 塞进 read_stream

### POST 响应的两种格式

服务端可以选择两种格式返回 POST 响应（streamable_http.py:364-374）：
```python
content_type = response.headers.get("content-type")
if content_type.startswith("application/json"):
    await self._handle_json_response(...)    # 一次性 JSON 响应
elif content_type.startswith("text/event-stream"):
    await self._handle_sse_response(...)     # SSE 流式响应（可以推送多个事件）
```
简单请求通常返回 JSON（效果和普通 HTTP 一样），复杂/长时间请求可以返回 SSE 流式推送进度。

### post_writer 内部：同一个协程做 send + recv + 转手

post_writer 对每个 JSONRPCRequest 启动独立 Task（streamable_http.py:568）：

```python
# post_writer 主循环
async for session_message in write_stream_reader:   # 从 write 通道读待发消息
    if isinstance(message.root, JSONRPCRequest):
        tg.start_soon(handle_request_async)         # ← 每个请求独立 Task

# 每个 Task 内部（_handle_post_request）：
async with client.stream("POST", url, json=msg) as response:    # TCP send
    content_type = response.headers.get("content-type")
    if content_type == "application/json":
        content = await response.aread()                          # TCP recv（等服务端返回）
        message = JSONRPCMessage.parse(content)
        await read_stream_writer.send(SessionMessage(message))   # 转手塞进 read 通道
    elif content_type == "text/event-stream":
        async for sse in event_source.aiter_sse():               # TCP recv（逐条读 SSE）
            message = JSONRPCMessage.parse(sse.data)
            await read_stream_writer.send(SessionMessage(message))  # 每条都转手
```

每个 Task 做三件事：TCP send → TCP recv → 塞进内存通道。HTTP 层面和普通请求-响应完全一样。

### 为什么要多一层转发（职责分离）

如果每个 POST Task 自己处理响应，不经过 read 通道，那么：
- SSE 模式下，POST 响应里可能夹带进度通知等非结果消息，每个 Task 都要写分发逻辑
- GET 长连接的通知也需要同样的分发逻辑
- N 个 POST Task + 1 个 GET Task = N+1 处重复代码

统一塞进 read 通道后，_receive_loop 一处分发：
- Response/Error → 按 id 路由到 per-request stream
- Notification → 分发给 notification handler
- Request → 分发给 request handler

职责分离：**Task C 只搬运（TCP → 内存通道），Task B 只分发（内存通道 → 各个等待者）。**

### 完整的三次唤醒链路

一次 call_tool 经历三次协程挂起和唤醒：

```
第 1 次唤醒（IO 唤醒 Task C）：
  Task C: await response.aread()
  → 协程挂起，注册 socket fd 到 select
  → 服务端处理完毕，TCP 数据到达
  → select 返回 → fd 回调 → Task C 醒来
  → 拿到 HTTP 响应，解析出 message
  → await read_stream_writer.send(message)   ← 塞进 read 通道

第 2 次唤醒（内存通道唤醒 Task B）：
  Task B (_receive_loop): async for message in self._read_stream
  → 之前挂起在 read_stream.receive()
  → Task C 的 send 触发内部回调 → Task B 醒来
  → 判断类型：是 id:42 的 Response
  → stream = response_streams.pop(42)
  → await stream.send(result)               ← 塞进 per-request 通道

第 3 次唤醒（内存通道唤醒 Task A）：
  Task A (send_request): await response_stream_reader.receive()
  → 之前挂起等结果
  → Task B 的 send 触发内部回调 → Task A 醒来
  → 拿到最终结果，返回给 call_tool
```

数据传递路径：
```
TCP socket → Task C（搬运工） → read 内存通道 → Task B（调度员） → per-request 通道 → Task A（调用者）
```

代价是多了两次协程切换（微秒级），换来的是分发逻辑只写一份。

---

### TCP socket 的双缓冲区

一个 TCP socket 在内核里有两块独立的缓冲区（全双工）：

```
进程                        内核                              对方
  send(data) ──→   [ 发送缓冲区 (send buf) ] ──→ 网卡 ──→ 对方 recv
  recv()     ←──   [ 接收缓冲区 (recv buf) ] ←── 网卡 ←── 对方 send
                      同一个 socket fd
```

- `send()` 把数据拷贝到发送缓冲区，内核负责后续发包、等 ACK、重传
- `recv()` 从接收缓冲区读数据，内核已经把收到的包排好序放在那了
- 两块缓冲区互不干扰，用同一个 fd 既能 send 又能 recv

### HTTP 的本质

HTTP 就是最普通的 TCP 一问一答：
```
客户端 socket.send(请求) → 服务端处理 → 服务端 socket.send(响应) → 客户端 socket.recv(响应)
```
不是批量，不是汇总，就是一来一回。

`requests.post(url, data)` 内部 = `socket.send(request_bytes)` + `socket.recv(response_bytes)`，封装成一个函数调完就拿到结果。

MCP SDK 的所有复杂性（read/write 拆分、内存通道、read loop 分发）都是 **SDK 在客户端内部加的抽象层**，跟 HTTP 协议本身无关。HTTP 那层的行为从头到尾没有变。

---

## 13. 并发连接的两阶段模式

当前项目在 `load_mcp_tools()` 中使用两阶段模式并发建连接：

```python
# Pass 1：全部 start，并发提交到 MCP loop（不等）
for cfg in configs:
    client.start()        # run_coroutine_threadsafe，立刻返回
    pending.append(...)

# Pass 2：统一等待就绪 + 注册工具
for cfg, client in pending:
    client.wait_ready()   # 此时可能已经连好了
    tool_list = client.list_tools()  # 这个是串行的（fut.result 阻塞）
```

- **连接建立是并行的**：3 个 server 各 2 秒 → 总共约 2 秒（取决于最慢的）
- **list_tools 是串行的**：每个 `fut.result()` 阻塞主线程，下一个要等前一个完成
- 优化方向：可以先全部提交 list_tools 的 future，再统一 `fut.result()`

---

## 14. 独立线程的根本原因：executor 是同步的

### 死锁分析
```
假设 MCP loop 在主线程：

1. agent_loop 调 _executor（同步函数调用）
2. _executor 调 call_tool（同步函数）
3. call_tool: fut = run_coroutine_threadsafe(session.call_tool(...), 主loop)
4. call_tool: fut.result()  ← 阻塞主线程
5. 主线程卡住 → 主 loop 也卡住 → session.call_tool 协程在队列里等调度
6. 没人能执行协程 → 没人能 set future → 永远等不到结果 → 死锁
```

### 关键区分：await vs fut.result()
```
await future：暂停协程，让出控制权，event loop 继续转 → 不死锁
fut.result()：阻塞整个线程，event loop 也停了 → 死锁
```

### 可选的改进方案
MCP SDK 的 `ClientSession.call_tool` 本身是 `async def`。项目中其他工具的 executor 已经是 async 的。
如果把 MCP executor 也改成 `async def`，直接 `await session.call_tool()`，就不需要：
- `run_coroutine_threadsafe` 同步包装
- `fut.result()` 阻塞等待
- 独立线程 + 独立 event loop

改后 MCP 和主业务共享主 event loop，架构更简洁。代价是失去故障隔离性。

---

## 15. 挂起的协程开销：几乎为零

### await 挂起的协程占什么？
- **内存**：一个协程帧对象，几百字节
- **CPU**：零。不参与 event loop 的每轮迭代

### event loop 每轮做什么？
```
while True:
    处理就绪队列的回调         ← 只有真正就绪的
    检查 I/O 事件（epoll 一次调用）← 不管监听多少 fd
    都没有 → 挂起等待
```

100 个 MCP 连接都在 await → event loop 每轮检查 I/O → 没事件 → 跳过。**挂着不花钱，只有真正有数据到时才消耗资源。**

### 结论
即使把 MCP 合并到主 event loop，大量挂起的连接也不会让 loop 臃肿。独立线程的价值纯粹是故障隔离，不是性能考虑。

---

## 16. Event Loop 底层：select、定时器、线程挂起

### event loop 每轮迭代的真实代码（CPython _run_once）
```python
def _run_once(self):
    # 1. 计算最近定时器的超时时间
    timeout = self._scheduled[0]._when - self.time()
    
    # 2. 带超时地等 I/O（同步阻塞，挂起线程）
    event_list = self._selector.select(timeout)
    
    # 3. 醒来后检查定时器，到期的放进就绪队列
    while self._scheduled and self._scheduled[0]._when <= now:
        handle = heapq.heappop(self._scheduled)
        self._ready.append(handle)
    
    # 4. 执行就绪队列里所有回调
    while self._ready:
        handle = self._ready.popleft()
        handle._run()    # callback → set_result → 协程恢复
```

**`select(timeout)` 是同步阻塞调用，会挂起线程。** 但这是 event loop 唯一应该阻塞的地方——走到这一步说明所有协程都在 await，没有代码需要 CPU。

### asyncio.sleep 的实现原理
```python
async def sleep(delay):
    future = loop.create_future()
    when = loop.time() + delay                               # 用时间戳算触发时间
    timer = TimerHandle(when, future.set_result, (None,))
    heapq.heappush(loop._scheduled, timer)                   # 插入定时器堆
    await future                                              # 协程暂停
```

定时器不是注册给操作系统的独立 timer，而是利用 `select(timeout)` 的超时参数：
- 所有定时器放进 Python 的堆（内存操作，极快）
- 取最近的那个算 timeout
- `select(timeout=最近的)` — 1 次系统调用覆盖所有定时器
- 醒来后批量检查哪些到期

### await sleep vs time.sleep
```
time.sleep(5)：
  操作系统挂起线程 5 秒
  唤醒通道：只有定时器
  I/O 来了也不理 → 所有协程都卡了

await asyncio.sleep(5)：
  注册定时器 → 协程暂停 → select(timeout=5)
  唤醒通道：定时器 + 所有监听的 fd
  I/O 来了 → 立刻醒来服务其他协程 → 回来继续等
```

### 线程挂起是常态
```
select(timeout) → 线程挂起
  I/O 到了 → 醒来
  执行回调 → 协程恢复 → 跑几行 → await → 暂停
  就绪队列空了
select(timeout) → 线程又挂起了
  定时器到了 → 醒来
  执行回调 → 协程恢复 → 跑几行 → await → 暂停
  就绪队列空了
select(timeout) → 线程又挂起了
```

线程大部分时间都在 select 里挂着。醒来干一小会活（微秒级），又挂起。**挂起是 event loop 的正常待机状态，不是异常。**

### 就绪队列（_ready）的机制
```python
self._ready = deque()   # 就是一个普通双端队列
```

什么时候加东西：
- I/O 就绪 → select 返回 → callback 加进 _ready
- 定时器到期 → callback 加进 _ready
- call_soon(callback) → 直接加进 _ready

**_ready 空了 = 没有代码需要 CPU = select 挂起等事件。** 没有复杂判断。

---

## 17. 跨线程唤醒：self-pipe 机制

### 问题
线程阻塞在 `select()` 里，只有 I/O 和 timeout 能唤醒。其他线程添加了 task，怎么唤醒？

### 解决方案：self-pipe
```python
# event loop 启动时
self._ssock, self._csock = socket.socketpair()   # 创建一对 socket
# 把 _ssock 注册到 select 监听
```

```python
# call_soon_threadsafe 源码
def call_soon_threadsafe(self, callback, *args):
    self._ready.append(handle)    # 1. 回调加进就绪队列
    self._write_to_self()         # 2. 唤醒 select

def _write_to_self(self):
    self._csock.send(b'\0')       # 往 self-pipe 写一个字节
```

### 唤醒流程
```
event loop 线程                      其他线程
────────────────                    ──────────
select(监听=[业务fd, _ssock])       
  线程挂起...                       call_soon_threadsafe(callback)
                                      _ready.append(callback)
                                      _csock.send(b'\0')
                                             │
  _ssock 收到数据！ ←────────────────────────┘
  select 立刻返回
  线程醒来
  检查 _ready → 有 callback → 执行
```

**把跨线程通知伪装成 I/O 事件。** select 分不清这个字节是网络来的还是自己线程写的，反正有数据就醒。

### 所有跨线程操作都走这条路
```python
# run_coroutine_threadsafe 内部
def run_coroutine_threadsafe(coro, loop):
    future = concurrent.futures.Future()
    def callback():
        task = loop.create_task(coro)
    loop.call_soon_threadsafe(callback)  # ← 最终都走这个
    return future
```

不管是添加 task、提交回调、还是 event.set，只要跨线程，最终都是：
加进队列 + 写 self-pipe 唤醒 select。

### 三种唤醒来源统一成一种机制
```
网络数据到了   → fd 可读    → select 返回
定时器到期     → timeout    → select 返回
其他线程通知   → self-pipe  → select 返回
```
全部变成 select 能感知的事件，一个入口处理所有情况。

---

## 18. Future 的本质：纯 Python 对象，不涉及操作系统

### 源码分析
```python
# await future 时
def __await__(self):
    if not self.done():
        yield self              # 把自己交给 Task.__step，协程暂停
    return self.result()

# set_result 时
def set_result(self, result):
    self._result = result
    self._state = _FINISHED
    self.__schedule_callbacks()  # 把回调加进就绪队列

# __schedule_callbacks
def __schedule_callbacks(self):
    for callback, ctx in self._callbacks:
        self._loop.call_soon(callback, self)   # 加进 _ready，仅此而已
```

**Future 不创建 fd，不注册 I/O 监听器，不注册定时器。** 它就是一个有 `_callbacks` 列表和 `_result` 的普通 Python 对象。

`set_result` 只做一件事：把回调用 `call_soon` 加进 `_ready` 队列。如果线程正在执行回调（醒着），下一轮就会处理；如果线程挂在 select 里，需要其他机制（I/O/定时器/self-pipe）先唤醒线程。

---

## 19. 各种 await 的完整对比

### 只注册到操作系统的东西才能唤醒线程

```
注册 fd 到 select 的（能直接唤醒线程）：
  1. 真实 I/O：socket.read/write → 注册 socket fd
  2. self-pipe：跨线程通知 → 注册 self-pipe fd
  只有这两种

不注册 fd 的：
  - asyncio.Future → 纯 Python 对象，操作 _ready 队列
  - asyncio.Event → 内部就是 Future
  - asyncio.sleep → 定时器堆 + select 的 timeout 参数（不是 fd）
```

### 各种 await 的唤醒方式

```
                        有定时器？  有 fd？   唤醒方式
await future             没有       没有     只能靠别的事件间接触发 set_result
await sleep(5)           有（5秒）  没有     操作系统定时器（select timeout）
await wait_for(f, 30)    有（30秒） 没有     定时器到期 或 future 被 set（先到的算）
await socket.read()      没有       有（fd） 操作系统 I/O 通知
```

### 完整执行链路
```
所有 await 都是：
  协程暂停 → 回到 event loop → 执行其他就绪回调 → _ready 空了
  → select(timeout=最近的定时器, fds=[所有I/O fd + self_pipe])
  → 线程挂起
  → 任何一个触发（I/O/定时器/self-pipe）→ 线程醒来
  → event loop 分发：执行就绪回调 → 可能 set_result → 恢复对应协程
```

### 关键认知
- **所有 await 底层走同一条路**：协程暂停 → event loop → select
- **Future 本身不能唤醒线程**：它只操作 _ready 队列
- **能唤醒线程的只有三样**：I/O fd 就绪、select timeout 到期、self-pipe 写入
- **sleep 和 wait_for 的 timeout 机制完全相同**：都是 call_later 注册定时器 → select timeout

---

## 20. 跨线程协程唤醒的完整原理（to_thread / run_in_executor）

### 核心问题
`await asyncio.to_thread(input, "> ")` 把同步函数扔到线程池执行，主线程协程 await 等待结果。线程池线程完成后，如何跨线程唤醒主线程的协程？

### 关键约束
- `asyncio.Future` 不是线程安全的，不能跨线程直接 `set_result`
- 主线程可能挂在 `select` 里，需要 I/O 事件才能唤醒

### to_thread 内部做了什么
```python
async def to_thread(func, *args):
    loop = get_running_loop()
    return await loop.run_in_executor(None, func, *args)

def run_in_executor(self, executor, func, *args):
    concurrent_future = executor.submit(func, *args)      # 提交到线程池
    return wrap_future(concurrent_future, loop=self)       # 桥接两种 future

def wrap_future(concurrent_future, loop):
    asyncio_future = loop.create_future()                  # 创建 asyncio.Future
    _chain_future(concurrent_future, asyncio_future)       # 关键：桥接
    return asyncio_future
```

### _chain_future：桥接两种 Future
```python
def _chain_future(source, destination):
    # source = concurrent.futures.Future（线程池的）
    # destination = asyncio.Future（主线程 event loop 的）

    def _call_set_state(source):
        if dest_loop is events._get_running_loop():
            _set_state(destination, source)              # 同线程：直接 set
        else:
            dest_loop.call_soon_threadsafe(_set_state, destination, source)
            #         ^^^^^^^^^^^^^^^^^^^^
            #         跨线程：用 call_soon_threadsafe！

    source.add_done_callback(_call_set_state)
    # concurrent_future 完成时，在线程池线程里触发这个回调
    # 回调持有 asyncio_future 的引用
```

### 完整唤醒流程
```
主线程                                    线程池线程
──────                                   ──────────
asyncio_future = create_future()
concurrent_future = executor.submit(input)  → 线程开始执行 input("> ")
_chain_future: 给 concurrent_future
  注册回调（持有 asyncio_future 引用）
await asyncio_future → 协程暂停
就绪队列空了
select(fds=[self_pipe]) → 线程挂起        用户还没输入...

                                          用户输入 "hello"
                                          input() 返回
                                          concurrent_future 完成
                                          触发 _call_set_state 回调：
                                            检测到跨线程
                                            call_soon_threadsafe(
                                              _set_state, asyncio_future, ...
                                            )
                                              ├ _ready.append(回调)
                                              └ self_pipe.send(b'\0')
                                                       │
self_pipe 有数据 → select 返回 ←───────────────────────┘
线程醒来
执行 _ready 里的回调
→ _set_state(asyncio_future, "hello")
→ asyncio_future.set_result("hello")
  （这是主线程在执行，不是跨线程！）
→ 协程恢复回调加进 _ready
→ 执行 → 协程恢复
→ await 解开，拿到 "hello"
```

### 核心原理总结
1. **主线程把 asyncio_future 的引用通过回调传给线程池线程**
2. **线程池线程完成后，不直接 set_result（不线程安全）**
3. **而是用 `call_soon_threadsafe` 把 set_result 包装成回调传回主线程**
4. **`call_soon_threadsafe` 写 self-pipe 唤醒主线程**
5. **主线程醒来，自己执行 set_result，协程恢复**

`set_result` 始终在主线程执行，不违反 Future 不线程安全的约束。其他线程只是"通知"主线程该干活了（写 self-pipe），真正的 set_result 是主线程自己干的。

这就是 **跨线程协程唤醒的通用原理**——所有跨线程唤醒（`to_thread`、`run_in_executor`、`run_coroutine_threadsafe`、MCP 的 `call_tool`）都走同一条路：`call_soon_threadsafe` + self-pipe + 主线程执行回调。

---

## 21. threading.Event vs asyncio — 两套完全独立的机制

### 核心区别：操作系统原语不同

`threading.Event` 和 asyncio 的 Future/Event 是**完全不同的体系**，底层用的 OS 原语不同，互不干扰：

| | asyncio（协程调度） | threading（线程调度） |
|---|---|---|
| 挂起 | `await future` → 协程暂停，event loop 继续 | `evt.wait()` → 线程挂起，event loop 停止 |
| 唤醒 | `future.set_result()` → 回调入 `_ready` 队列 | `evt.set()` → OS 内核唤醒线程 |
| 调度者 | event loop（`_run_once`） | OS 内核 |
| 精确匹配 | 每个 Future 有自己的回调链 | 每个 Event 有自己的 OS 条件变量 |
| 经过 select？ | 是（跨线程时通过 self-pipe） | **否，完全不经过** |
| 底层 OS 原语 | `select` / `epoll`（I/O 多路复用） | `pthread_cond` / `WaitForSingleObject` |

### threading.Event 的唤醒是排他的

`threading.Event.wait()` 挂起线程后，**只接受一个唤醒条件**：

```
同一个 Event 对象调用 .set()   → 唤醒，返回 True
timeout 到期                   → 唤醒，返回 False
```

其他任何事情都唤醒不了它——网络数据到达、其他 socket 可读、其他线程 `call_soon_threadsafe`、定时器到期，统统不行。

对比 `select` 的多路复用：
```
select(timeout, [fd1, fd2, self_pipe])
  → 任何一个 fd 就绪都会唤醒
  → timeout 到期也会唤醒
  → 多个唤醒源

threading.Event.wait(timeout)
  → 只有 .set() 能唤醒
  → timeout 到期也会唤醒
  → 就这两种，没有第三种
```

### 在项目中的使用场景

```python
# src/mcp_tool/base.py
def wait_ready(self, timeout: float = 30) -> None:
    self._ready_event.wait(timeout=timeout)   # threading.Event，不是 asyncio.Event
```

调用链：`register_mcp_tools` → `load_mcp_tools` → `client.wait_ready()` → `threading.Event.wait()`

```
主线程                              MCP 后台线程
  │                                      │
  │  client.start()                      │
  │    → run_coroutine_threadsafe   ───→ │ 开始 _async_lifecycle
  │                                      │   建立连接...
  │  client.wait_ready()                 │   
  │    → self._ready_event.wait()        │
  │      主线程挂起（OS级）               │
  │      和 event loop 无关              │   连接成功！
  │      和 select 无关                  │   self._ready_event.set()
  │      和 Future 无关                  │        │
  │          ↑                           │        │
  │          └── OS 内核唤醒 ←───────────────────┘
  │      主线程恢复                       │
```

**安全条件**：`wait_ready` 只在启动阶段调用（`async_main` 最开头），此时 event loop 上没有其他协程。如果在运行时调用，会冻结整个 event loop。

---

## 22. 线程阻塞期间的信号累积与恢复

### 问题：event loop 冻住时，信号去哪了？

当主线程被 `threading.Event.wait()` 阻塞时，event loop 完全停止（`_run_once` 不会被调用，`select` 也不会被调用）。但此期间产生的各种信号**不会丢失**，它们累积在各自的缓冲区里：

```
信号类型               堆积位置                        恢复后处理方式
──────────────────────────────────────────────────────────────────
网络数据到达            OS 内核的 socket 接收缓冲区      select 返回 fd 可读
self-pipe 写入         pipe 的内核缓冲区                select 返回 self_pipe 可读
call_soon_threadsafe   _ready 队列（Python list）       _run_once 直接执行
定时器到期             _scheduled 堆（仍在堆中）         _run_once 检查时发现已过期
```

### 恢复流程

```
threading.Event.set()
    ↓
主线程醒了 → wait_ready() 返回
    ↓
register_mcp_tools() 继续 → 最终进入 agent loop
    ↓
event loop 恢复运转 → _run_once 开始循环
    ↓
第一次 select()：
  → 发现 socket_fd 可读（数据早就到了）
  → 发现 self_pipe 可读（字节早就写了）
  → 全部一次性返回
    ↓
处理就绪队列：
  → 累积的回调集中执行
  → 过期的定时器集中触发
  → 几轮 _run_once 后一切恢复正常
```

### 关键结论
1. **信号不会丢失**——各有各的缓冲区，OS 内核/Python 数据结构都会保存
2. **但信号会延迟**——阻塞期间得不到及时响应
3. **恢复后集中处理**——第一轮 `_run_once` 把累积的事件批量消化
4. **这就是 `threading.Event.wait()` 只适合启动阶段的原因**——运行时用会导致所有协程的响应被延迟

---

## 23. OS 内核线程挂起与唤醒的本质 — 等待队列隔离

### 核心原理：一个线程一次只能挂在一个等待队列

OS 内核中，线程进入 SLEEPING 状态有多种原因，每种对应独立的等待队列：

```
线程 SLEEPING 的原因           挂在哪个队列              唤醒条件
─────────────────────────────────────────────────────────────────
select/epoll（等 I/O）        fd 等待队列               硬件中断（网卡/磁盘数据到达）
pthread_cond_wait（条件变量）   cond 等待队列             其他线程 pthread_cond_signal
pthread_mutex_lock（互斥锁）   mutex 等待队列            其他线程 pthread_mutex_unlock
sem_wait（信号量）             semaphore 等待队列        其他线程 sem_post
nanosleep（定时等待）          定时器队列                硬件时钟中断
```

这些队列**彼此完全隔离**，不存在优先级关系。线程挂在哪个队列，就只响应那个队列的唤醒信号。

### 关键推论：其他事件不会"排队等触发"

```
线程A 调了 threading.Event.wait()
  → 挂在 cond 等待队列
  → 没有注册在任何 fd 等待队列上

此时网卡收到数据：
  内核查 fd 等待队列 → 线程A 不在 → 跟线程A完全无关
  数据留在 socket 缓冲区（内核存着，不会丢）
  但不是"排队等线程A恢复后触发"
```

数据不丢，但原因是**缓冲区保存**，不是事件排队：

```
.set() → 线程A 从 cond 等待队列恢复
    ↓
后续代码重新调 select()
    ↓
线程A 重新注册到 fd 等待队列
    ↓
select 立刻发现缓冲区有数据 → 返回 → 处理
```

是**重新注册后才发现之前积攒的数据**，不是之前的事件在排队等你。

### threading.Event.set() 的底层路径

```
线程B 调 self._ready_event.set()
    ↓
Python: _flag = True → pthread_cond_signal() 系统调用 → 进入内核态
    ↓
OS 内核：查这个条件变量的等待队列 → 找到线程A → 移回运行队列
    ↓
纯软件操作，无硬件参与，无文件描述符，无 I/O
```

与 I/O 唤醒对比：I/O 靠硬件中断触发，条件变量靠另一个线程的系统调用触发。两条完全独立的内核路径。

---

## 24. 进程间通信：管道（pipe）vs 文件 vs socket

### 管道的本质

管道就是内核里的一块缓冲区，一端写另一端读：

```
os.pipe() → 内核创建缓冲区，返回两个 fd
  r, w = os.pipe()
  os.write(w, b"hello")   → 数据进缓冲区
  os.read(r, 1024)        → 从缓冲区读出 b"hello"
```

管道是**单向的**，双向通信需要两根管道。

### subprocess 的 fork + dup2 + exec 流程

`subprocess.Popen(cmd, stdin=PIPE, stdout=PIPE)` 底层做的事：

```
1. 创建两根管道
   r1, w1 = os.pipe()   # 父→子
   r2, w2 = os.pipe()   # 子→父

2. fork() — 子进程复制整张 fd 表

3. 子进程 dup2 偷梁换柱
   dup2(r1, 0)  → fd 0 (stdin) 指向管道1读端
   dup2(w2, 1)  → fd 1 (stdout) 指向管道2写端

4. exec("node server.js")
   新程序继承 fd 表
   读 stdin = 读管道（不知情）
   写 stdout = 写管道（不知情）
```

子进程**不需要任何管道代码**，它以为在读终端，其实在读管道。stdin 就是 fd 0，这是写死的约定。

### fd 表

每个进程维护一张表，记录打开了哪些东西：
```
fd 0 → 终端输入（stdin）   ← 固定约定
fd 1 → 终端输出（stdout）  ← 固定约定
fd 2 → 终端错误（stderr）  ← 固定约定
fd 3 → 打开的文件/socket/pipe...（按顺序分配）
```

`dup2(old, new)` 的作用：让 new 指向 old 指向的同一个内核对象。

### fork + exec 是 Unix 创建进程的唯一方式

```
Unix：  fork()（复制进程）+ exec()（替换程序）= 两步组合
Windows：CreateProcess()  = 一步到位
Python subprocess 封装了这两步
```

fork 只复制调用线程，不复制其他线程。fork 后通常立刻 exec 替换成新程序。

### stdout 被管道占用后，子进程用 stderr 输出日志

```
子进程的三个 fd：
  fd 0 (stdin)  → pipe（接管，接收请求）
  fd 1 (stdout) → pipe（接管，发送响应）
  fd 2 (stderr) → 终端（没动，打日志）
```

### 为什么进程通信用管道而不用文件

```
              管道                    文件监听
延迟         纳秒级                   毫秒级（轮询或通知延迟）
同步         内核保证                  需要自己加锁
数据完整性    read 拿到就是完整的       可能读到写一半的
select 支持  原生支持                  不支持（文件 fd 永远"就绪"）
清理         进程退出自动销毁           文件留在磁盘要手动删
```

普通文件在 select/epoll 里**永远返回"可读"**，因为数据就在磁盘上，不存在"等待到达"的概念。管道/socket 是流，有"数据到达"的时间维度，天然支持阻塞等待。

### 文件监听用的是独立的内核机制

```
select/epoll：I/O 子系统，监听 socket/pipe 的数据流
inotify（Linux）/ ReadDirectoryChangesW（Windows）：文件系统通知，独立的内核 API
```

文件监听适合的场景：

```
文件监听：配置/资源变了 → 全量重载（低频、全量、不关心中间过程）
管道/socket：持续的实时流式通信（高频、增量、需要顺序和完整性）
```

---

## 25. watchdog Observer + threading.Timer + 跨线程桥接

### Observer 线程的工作原理

watchdog 的 Observer 是独立线程，用文件系统专用 API 阻塞等待变化：

```
Observer 线程（纯同步，没有 event loop）：
  Windows: ReadDirectoryChangesW(directory_handle, ...)
  → 线程阻塞，等 OS 通知目录变化
  → 文件变了 → 唤醒 → 调 handler.on_any_event()
  → 继续阻塞等下一次变化
```

不是 select/epoll 的 fd 注册，是文件系统专用的通知 API。但"挂起→等待→唤醒→处理"的模式和 I/O 监听类似。

### threading.Timer 的本质：创建新线程

```python
self._timer = threading.Timer(self._delay, self._fire)
self._timer.start()
```

`threading.Timer` 继承自 `threading.Thread`，源码简化：

```python
class Timer(Thread):
    def run(self):
        self.finished.wait(self.interval)    # threading.Event.wait → 线程挂起 N 秒
        if not self.finished.is_set():
            self.function()                   # 执行回调
```

不是 asyncio 定时器，不是 event loop 的 call_later，不走 select timeout。就是开一个新线程，用 `threading.Event.wait(N)` 等待（OS 条件变量挂起），时间到了执行回调。

### 为什么 Timer 不用 asyncio 的 call_later？

因为 Observer 线程里**没有 event loop**，不能直接用 asyncio 的任何东西。

但其实可以优化——用 `call_soon_threadsafe` 把定时器提交到主线程 event loop：

```python
# 当前方案（3 个线程）：
Observer 线程 → 创建 Timer 线程 → 等 0.5s → call_soon_threadsafe → 主线程

# 优化方案（2 个线程）：
Observer 线程 → call_soon_threadsafe → 主线程 call_later → 等 0.5s → 执行
```

### 三种定时器对比

```
threading.Timer：
  创建新线程 → threading.Event.wait(N) → 执行回调
  底层：OS 条件变量挂起

asyncio call_later / await sleep：
  注册到 event loop 的定时器堆
  底层：select(timeout) 超时返回 → 检查堆 → 执行回调

OS 硬件定时器：
  硬件时钟中断 → 内核更新系统时间 → 驱动上面两种机制的时间判断
```

### 完整事件流

```
Observer 线程              Timer 线程            主线程 event loop
     │                         │                       │
  ReadDirectoryChangesW        │                       │
  （线程阻塞等文件变化）          │                       │
     │                         │                       │
  文件变了！OS 唤醒              │                       │
  on_any_event()               │                       │
  trigger()                    │                       │
    → Timer(0.5s, _fire)  ───→ │ 新线程启动              │
     │                         │ Event.wait(0.5)       │
  继续监听...                   │ （OS条件变量挂起）       │
     │                         │                       │
     │                         │ 0.5秒到了              │
     │                         │ _fire()               │
     │                         │ call_soon_threadsafe ──→ 写 self-pipe
     │                         │ （线程结束）             │ select 唤醒
     │                                                 │ 执行 _reload_skills
```

---

## 26. TCP 传输可靠性：应用层不需要处理丢包

### TCP 内核自动处理的事情

TCP 的 ACK 和重传完全在内核层面完成，应用层无感：

```
发送端内核                        接收端内核
  发包1（序号1-100）──────→      收到，回 ACK 101
  发包2（序号101-200）→ 丢了     没收到
  发包3（序号201-300）──────→    收到，但前面缺了，回 ACK 101（重复）
  
  发现 ACK 101 重复 → 自动重发包2 ──→  收到！缓冲区补齐，回 ACK 301
  
  应用层 write() 早就返回了         应用层 read() 拿到完整有序数据
  根本不知道中间丢过包              根本不知道中间丢过包
```

`write()` 只是把数据拷贝到内核发送缓冲区就返回了，后面的发包、等 ACK、重传全是内核自己干的。本质上和 asyncio 一样——把活丢给内核，自己继续干别的。

### 滑动窗口：不是发一个等一个

```
实际 TCP（滑动窗口）：
  发包1 → 发包2 → 发包3 → 发包4   （连续发，不等 ACK）
       ← ACK1 ←                   （ACK 陆续回来）
  发包5 →                          （窗口滑动，继续发）
```

窗口大小决定可以同时在路上多少个包。

### 应用层只会看到两种结果

```
正常：数据完整有序到达（中间丢了多少包不知道）
异常：ConnectionError — 连接断了（内核重传几分钟都救不回来）
没有中间状态
```

### 连接断了怎么办

应用层自己处理，通常就是重连：
```
LLM 流式：重连 → 重新发请求 → 从头生成
股票行情：重连 → 自动推最新价格
文件下载：重连 → 断点续传（Range 头）
```

---

## 27. 粘包与半包：TCP 字节流没有消息边界

### 为什么会粘包

TCP 不知道应用层的"消息"是什么，它只看到字节流：

```python
sock.send(b'{"id":1}\n')    # 应用层认为这是"包1"
sock.send(b'{"id":2}\n')    # 应用层认为这是"包2"

# TCP 看到的：就是 20 个字节的流
# 内核可能合并发送（Nagle 算法），也可能拆分发送
```

接收端 `read` 可能收到的情况：
```
粘包：{"id":1}\n{"id":2}\n          多条粘在一起
半包：{"id":1}\n{"id":2             一条完整 + 一条的前半
理想：{"id":1}\n  然后  {"id":2}\n   刚好按消息切分（不保证）
```

### 关键：数据一定是完整有序的

粘包和半包**不是错误**，只是"一次 read 读到多少不确定"。数据本身一个字节都不会少，也不会乱序。

不存在"收到半条消息然后另一半丢了"的情况——那是 UDP 的问题。TCP 下：
```
第一次 read：{"id":1}\n{"id":2     ← 包2读了一半
第二次 read：}\n                    ← 剩下的一定在这里
```

### 应用层处理：缓冲区拼接 + 边界切分

```python
buffer = b""
while True:
    chunk = sock.read(4096)
    buffer += chunk
    while b"\n" in buffer:
        msg, buffer = buffer.split(b"\n", 1)
        handle(json.loads(msg))
    # buffer 里剩下的不完整部分，等下次 read 补齐
```

### 应用层只需要处理粘包，不需要处理丢包

```
TCP 下应用层需要处理：粘包（按边界切分）、半包（缓冲拼接）
TCP 下应用层不需要处理：丢包（内核重传）、错包（内核校验）、乱序（内核排序）
```

### 消息边界方案

```
换行分隔：  {"id":1}\n{"id":2}\n        按 \n 切分
长度前缀：  [4字节长度][消息体]           先读长度再读消息
Content-Length：HTTP 的方案             头部声明长度
结束标记：  data: [DONE]\n\n            特殊消息表示结束
```

### 协议写错导致的解析失败

如果发送端自己发了格式错误的数据（应用层 bug，不是传输问题）：
```
长度前缀协议：声明5字节实际3字节 → 永久错位 → 必须断开重连
换行分隔协议：JSON 格式错 → 丢弃这条 → 下一个 \n 可能恢复
```

TCP 忠实地把错误数据完整有序地传到了对方——传输没错，是内容有问题。

---

## 28. 应用层通信模式与确认策略

### 四种通信模式

**模式1：一来一回（请求-响应）**
```
→ 请求1 → 等响应 ← 响应1 → 请求2 → 等响应 ← 响应2
每条请求对应一个响应，串行执行
场景：MCP call_tool、HTTP API、银行转账
特点：下一步依赖上一步结果
```

**模式2：批量发送 + 统一确认**
```
→ 数据1 → 数据2 → 数据3 → done
← {"success": [1,3], "failed": [2]}
场景：批量数据导入、文件上传
特点：每条数据独立，互不依赖
```

**模式3：持续推送（单向，不需要确认）**
```
→ "订阅AAPL"
← AAPL:150.01 ← AAPL:150.02 ← AAPL:150.03 ...
场景：股票行情、LLM 流式 token、日志流
特点：数据会过期，丢一条不影响，TCP 保证到达就够了
```

**模式4：双向独立流（边发边收）**
```
发送流：→ 数据1 → 数据2 → 数据3 →   （不停发）
确认流：← OK(1) ← 错误(2) ← OK(3)   （陆续回）
场景：消息队列（Kafka）、高吞吐数据处理
特点：发送不等确认，靠 ID 匹配结果
```

### 选择标准

```
下一条依赖上一条结果？ → 一来一回（模式1）
每条独立且需要确认？   → 批量发 + 统一确认（模式2）
数据会过期不需要确认？ → 持续推送（模式3）
独立且要求高吞吐？     → 双向独立流（模式4）
```

### 流式场景如何知道传输结束

发送端通常不知道总共有多少条（比如 LLM 生成），所以不用头包声明总数：
```
方案1：特殊结束消息     data: [DONE]\n\n
方案2：头部声明总数     {"total": 3} 然后收够3条
方案3：关闭连接         发完 → 关连接 → read 返回空
```

### 模式4的错误重传策略

同一条流里混发原始数据和重传很复杂，实际工程中通常分开处理：
```
第一轮：发 id:1,2,3,done → 报告：id:2 失败
第二轮：只发 id:2 → 成功
分轮处理，不在同一条流里混发
```

### 模式1 vs 模式2 的时间对比

```
模式1（1000条数据，一来一回）：
  1000次请求 × (传输时间 + 处理时间 + 回传时间) = 很慢

模式2（1000条数据，批量发 + 统一确认）：
  连续传输时间 + 统一处理时间 + 一次回传 = 快得多
  省了999次等待回传的网络往返时间
```

---

## 29. MCP Streamable HTTP 完整架构解析（源码验证）

### 核心结论：HTTP 本身没变，SDK 在客户端加了一层抽象

MCP Streamable HTTP 不是新协议。HTTP 层面还是普通的请求-响应，服务端处理完在同一个 POST response 里返回结果。SDK 在客户端内部加了 read/write 拆分和内存通道做统一分发。

### read/write 不是 TCP 的 send/recv

`streamablehttp_client` 返回的 read/write 是**内存通道**（anyio.MemoryObjectStream），不是 socket：

```
write(msg)  → 往内存队列丢消息（不碰 socket）
read()      → 从内存队列读消息（不碰 socket）
post_writer → TCP send + TCP recv + 把结果塞进 read 队列（这里碰 socket）
```

真正的 TCP 操作藏在 SDK 内部的 post_writer Task 里。

### 为什么要拆开 read 和 write

不是因为 HTTP 需要，是因为 **read 通道要同时收两种来源的消息**：

```
来源 1：POST 的响应（工具调用结果）      ← 每个 POST Task 各自 TCP recv 后塞进来
来源 2：GET 长连接的推送（服务端主动通知）← GET handler 塞进来

如果不拆：
  每个 POST Task 自己处理 → 要在每个 Task 里写消息分类逻辑
  GET handler 也要写同样的分类逻辑
  → 重复代码

拆开后：
  所有来源 → 统一 read 通道 → _receive_loop 一处分类分发
  → 代码只写一份
```

### 每个 POST 是独立的 HTTP 请求

并发时不是共享一个 socket，是多个独立连接：

```python
# 源码 streamable_http.py:568
if isinstance(message.root, JSONRPCRequest):
    tg.start_soon(handle_request_async)   # ← 每个请求开独立 Task
```

每个 Task 做完整的 HTTP 一来一回：

```python
# _handle_post_request (streamable_http.py:340)
async with client.stream("POST", url, json=msg) as response:  # TCP send
    content = await response.aread()                            # TCP recv
    message = parse(content)
    await read_stream_writer.send(message)                      # 塞进 read 通道
```

### 完整的三次唤醒链路（一次 call_tool 的完整路径）

```
Task C（搬运工）：
  await response.aread()              ← 第 1 次挂起，等 TCP 数据
  网卡数据到达 → select 返回 → Task C 醒来
  解析结果 → await read_stream_writer.send(message)  ← 塞进 read 通道

Task B（_receive_loop 调度员）：
  async for message in read_stream    ← 之前挂起在这
  Task C 的 send 触发 Event.set()     → Task B 醒来
  判断类型 → 是 id:42 的响应
  response_streams[42].send(result)   ← 塞进 per-request 通道

Task A（call_tool 调用者）：
  await response_stream_reader.receive()  ← 之前挂起在这
  Task B 的 send 触发 Event.set()         → Task A 醒来
  拿到最终结果，返回

数据路径：TCP → Task C → read 通道 → Task B → per-request 通道 → Task A
```

代价是多两次协程切换（微秒级），换来分发逻辑只写一份。

### 内存通道的唤醒机制：Event.wait() + Event.set()

anyio 的 MemoryObjectStream 内部不用 Future（因为 trio 没有 Future），用 Event：

```python
# receive() 内部 (anyio/streams/memory.py:114)
receive_event = Event()
receiver = _MemoryObjectItemReceiver()
waiting_receivers[receive_event] = receiver
await receive_event.wait()       # ← 挂起
return receiver.item             # ← 醒来后取数据

# send_nowait() 内部 (anyio/streams/memory.py:222)
receive_event, receiver = waiting_receivers.popitem()
receiver.item = item             # ← 数据传递（赋值）
receive_event.set()              # ← 唤醒通知（不带数据）
```

Event 只负责通知"好了"，数据通过 receiver.item 单独传递。
等价于 Future 的 set_result，只是拆成了两步（赋值 + 唤醒）。

### 不需要锁

协程是协作式调度，同一个 event loop 同一时刻只有一个协程在跑：

```
Task C-1: receiver.item = data → event.set() → await（让出控制权）
Task B:   现在才轮到它 → event.wait() 返回 → 读 receiver.item（数据已经稳了）
Task C-2: 现在才轮到它 → send 下一条
```

不存在两个协程同时操作同一个 receiver 的情况。线程需要锁是因为 OS 可以随时抢占，协程只在 await 处主动让出，赋值操作中间不会被打断。

---

## 30. SSE 的本质：分块返回的 HTTP 响应

### SSE vs 普通 JSON 响应

```
普通 JSON：
  服务端处理完 → 一次性返回完整 JSON → 响应结束
  Content-Type: application/json

SSE：
  服务端边做边推 → 每完成一部分就发一条 → 最后关闭响应
  Content-Type: text/event-stream

  格式：每条消息前加 "data: "，用 \n\n 分隔
  data: {"progress": "30%"}\n\n
  data: {"progress": "70%"}\n\n
  data: {"id": 42, "result": "done"}\n\n
```

SSE 的 data 后面就是普通 JSON 字符串，没有特殊格式。

### SSE 的实现就是 generator

```python
# 服务端（Flask）
def generate():
    time.sleep(1)
    yield f"data: {json.dumps({'progress': '30%'})}\n\n"    # 第 1 秒推送
    time.sleep(1)
    yield f"data: {json.dumps({'result': 'done'})}\n\n"     # 第 2 秒推送
    # generator 耗尽 → Flask 关闭 response

return Response(generate(), content_type="text/event-stream")
```

Flask 内部就是 for 循环 + socket.send：
```
for chunk in generator:
    socket.send(chunk)      # 每次 yield 往 TCP 写一条
socket.close()              # generator 耗尽，关闭响应
```

yield 不是必需的，只是语法便利。不用 yield 就在每个位置手动 socket.send() 也一样。

### SSE 流中每条消息的处理

MCP SDK 中 SSE 响应的每条消息都**立刻**塞进 read 通道，不是攒完汇总：

```python
# 源码 streamable_http.py:407
async for sse in event_source.aiter_sse():          # 逐条读
    await self._handle_sse_event(sse, read_stream_writer)  # 每条立刻 send

# _handle_sse_event 内部 (streamable_http.py:228)
session_message = SessionMessage(message)
await read_stream_writer.send(session_message)      # 立刻塞进 read 通道
```

每条消息的完整路径：

```
SSE event（进度通知）：Task C recv → read 通道 → Task B → notification handler
SSE event（最终响应）：Task C recv → read 通道 → Task B → per-request 通道 → Task A
```

Task A（call_tool 调用者）只在收到属于自己 id 的响应时才醒来，中间的进度通知由 notification handler 单独处理。

---

## 31. 两种业务场景：普通 JSON vs SSE 流式

### 服务端根据场景选择返回格式

```python
# 源码 streamable_http.py:365-369
content_type = response.headers.get("content-type")
if content_type.startswith("application/json"):
    await self._handle_json_response(...)     # 简单工具 → 一次性 JSON
elif content_type.startswith("text/event-stream"):
    await self._handle_sse_response(...)      # 长任务 → SSE 流式
```

### 场景对比

```
普通 JSON 响应：
  客户端问 → 服务端处理完 → 一次性返回完整结果
  场景：list_tools、简单 call_tool、查数据
  客户端只关心最终结果

SSE 流式响应：
  客户端问 → 服务端边做边推 → 分多次返回
  场景：LLM 逐 token 生成、长任务进度、实时数据
  客户端需要中间状态
```

### 当前项目的实际情况

我们的 list_tools 和 call_tool 都是等完整结果才继续业务，属于普通 JSON 场景。
SDK 的 read/write 拆分、read loop 分发整套架构是为完整场景（SSE + 通知 + 并发）设计的。
对我们来说是"杀鸡用牛刀"——用到的只是最简单的 JSON 一问一答部分。

## 32. HTTP/2 vs HTTP/1.1 源码验证：read loop + stream_id 分发

### 核心结论

HTTP/2 在客户端加了和服务端一样的 read loop + stream_id 分发机制。
通过对 httpcore、h2、hyperframe 三个库的源码验证确认。

### HTTP/2 帧级别的 stream_id（hyperframe 源码）

每个 HTTP/2 帧都携带 stream_id，这是二进制帧头的一部分：
```python
# hyperframe/frame.py:57-60
class Frame:
    def __init__(self, stream_id: int, flags=()):
        self.stream_id = stream_id  # 每个帧都有

# 第135行：从二进制帧头解析 stream_id（32位）
stream_id = fields[4] & 0x7FFFFFFF
```

### HTTP/2 客户端的 read loop（httpcore 源码）

```python
# httpcore/_async/http2.py

# 第68-76行：按 stream_id 分桶的事件字典
self._events: dict[int, list[...]] = {}

# 第61行：读锁，同一时间只有一个协程读 TCP socket
self._read_lock = AsyncLock()

# 第342-388行：_receive_events —— 这就是 read loop
async def _receive_events(self, request, stream_id=None):
    async with self._read_lock:                          # 加锁
        events = await self._read_incoming_data(request)  # 从 TCP 读原始字节
        for event in events:                              # h2 解析出帧事件
            if isinstance(event, (ResponseReceived, DataReceived, ...)):
                if event.stream_id in self._events:
                    self._events[event.stream_id].append(event)  # 按 stream_id 分发

# 第327-339行：每个请求从自己的桶里取
async def _receive_stream_event(self, request, stream_id):
    while not self._events.get(stream_id):           # 我的桶里没数据？
        await self._receive_events(request, stream_id)  # 去读 TCP，顺便帮所有人分发
    return self._events[stream_id].pop(0)             # 从自己的桶里取
```

关键：HTTP/2 的 read loop 不是独立后台协程，而是**按需驱动**——哪个协程需要数据，就由它去读 TCP 并帮所有 stream 分发。

### h2 库的帧解析与分发（h2 源码）

```python
# h2/connection.py:1495
def receive_data(self, data: bytes) -> list[Event]:
    self.incoming_buffer.add_data(data)          # 原始字节丢进缓冲区
    for frame in self.incoming_buffer:           # 逐帧解析（帧是原子的）
        events.extend(self._receive_frame(frame))  # 按帧类型分发

# 第403-416行：帧类型分发表
self._frame_dispatch_table = {
    HeadersFrame: self._receive_headers_frame,    # 用 frame.stream_id 找对应 stream
    DataFrame: self._receive_data_frame,          # 用 frame.stream_id 找对应 stream
    SettingsFrame: self._receive_settings_frame,  # 连接级别，stream_id = 0
    PingFrame: self._receive_ping_frame,          # 连接级别
    GoAwayFrame: self._receive_goaway_frame,      # 连接级别
    ...
}
```

### HTTP/1.1 对比——没有这些东西（httpcore 源码）

```python
# httpcore/_async/http11.py
# 没有 _events 字典（不需要分发）
# 没有 _read_lock（不需要读锁）
# 没有 stream_id（不存在多路复用）
# is_available() 只在 IDLE 时返回 True —— 同一时间只有一个请求
```

### 对比总结

| | HTTP/1.1 | HTTP/2 |
|---|---|---|
| TCP 连接 | 每个请求独占（或串行复用） | 所有请求共享一个 |
| 帧头有 stream_id | 没有 | 有（32 位） |
| 客户端读取模式 | 直接读，直接用 | read loop + 按 stream_id 分桶分发 |
| 读锁 | 不需要 | `_read_lock` |
| `_events` 分发表 | 不需要 | `dict[stream_id, list[event]]` |
| 并发请求 | 不支持（同一连接上） | 支持（同一连接上多 stream） |

### HTTP/1.1 的连接复用

HTTP/1.1 默认开启 keep-alive，连接可以复用但是**串行**的：
```python
# httpcore/_async/http11.py:73
if self._state in (HTTPConnectionState.NEW, HTTPConnectionState.IDLE):
    self._state = HTTPConnectionState.ACTIVE  # 占住
else:
    raise ConnectionNotAvailable()  # 被别人占了

# 第239-245行：响应结束后释放
self._state = HTTPConnectionState.IDLE
self._h11_state.start_next_cycle()  # 准备复用
```
同时 10 个并发请求 → 连接池开 10 个 TCP 连接。HTTP/2 一个连接就够。

## 33. MCP 为什么不直接用 HTTP/2

### 答案：用不用都行，对 MCP 没影响

MCP 协议定义的是 HTTP 层的规范（POST 发请求、GET SSE 收通知），不关心底层是 HTTP/1.1 还是 HTTP/2。httpx 会自动处理协议升级，MCP 代码一行不改。

### HTTP/2 解决不了 MCP 的核心需求

HTTP（无论 1.1 还是 2）的根本规则：**服务端不能主动发数据，必须先有客户端请求。**

HTTP/2 多路复用只是让多个请求-响应共享一个 TCP 连接，但每个 stream 仍然由客户端发起。服务端不能凭空创建 stream 推数据。

所以 MCP 无论用 HTTP/1.1 还是 HTTP/2，都需要 GET SSE 来"钻空子"：
- 客户端发 GET 请求
- 服务端的响应永远不结束（不 return）
- 服务端在这个"永远不结束的响应"里随时塞通知

这个 SSE 机制在 HTTP/1.1 和 HTTP/2 上写法完全一样。

### HTTP/2 对 MCP 的唯一好处

POST 和 GET SSE 可能共享一个 TCP 连接，省了一次 TCP 握手。但 MCP 请求频率很低，这点优化几乎无感。

## 34. MCP SDK 的 read/write 内存通道：不是网络接口

### 常见误解

```python
read, write, _ = await stack.enter_async_context(
    streamablehttp_client(url=self._cfg.url)
)
session = ClientSession(read, write)
```

这里的 `read, write` **不是** TCP socket 的 read/write，**不是**封装过的 send/recv。

### 实际是什么

```python
# streamable_http.py:634-635
read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
write_stream, write_stream_reader = anyio.create_memory_object_stream(0)
```

就是两个内存队列（anyio.MemoryObjectStream），进程内的 Python 对象传递管道，类似 `queue.Queue` 的异步版。不经过网络，不经过序列化。

### 实际数据流

```
call_tool("search", {...})
  ↓
session 塞 JSON-RPC 消息进 write_stream（内存队列）
  ↓
post_writer 协程从 write_stream_reader 取出来
  ↓
post_writer 用 httpx 发真正的 HTTP 请求 ←── 这里才是 TCP 网络通信
  ↓
httpx 拿到 HTTP 响应
  ↓
post_writer 把响应塞进 read_stream_writer（内存队列）
  ↓
_receive_loop 从 read_stream 取出来，按 JSON-RPC id 分发给调用者
```

### 为什么要多这一层内存队列

**因为 ClientSession 要兼容四种传输方式，而其中三种必须用内存队列。**

四种传输的内部差异：
```
stdio:            一根 stdout 管子，所有消息混着来 → 必须统一 read + 分发
websocket:        一根 ws 连接，所有消息混着来   → 必须统一 read + 分发
旧版 SSE:         一根 GET SSE 流，所有消息混着来 → 必须统一 read + 分发
streamable_http:  POST 各自独立返回响应          → 其实可以不用队列
```

但四种传输都产出相同的 `(read_stream, write_stream)` 接口：
```python
# 四个文件各自独立实现，但接口统一
mcp/client/streamable_http.py  → (read_stream, write_stream)
mcp/client/sse.py              → (read_stream, write_stream)
mcp/client/websocket.py        → (read_stream, write_stream)
mcp/client/stdio/__init__.py   → (read_stream, write_stream)
```

`ClientSession` 只认识内存通道，完全不知道背后是哪种传输（策略模式）。
Streamable HTTP 为此多绕了一层队列，但换来了架构统一。

### 旧版 SSE Transport 的历史

MCP 早期版本的 SSE Transport：所有通信都走一根 GET SSE 长连接。
客户端 POST 发请求，但响应不从 POST 回来，而是从 GET SSE 流推回来。
```
旧版：POST → 202 Accepted（空的），响应从 GET SSE 推回来，跟通知混在一起
现在：POST → 响应直接回来，GET SSE 只用于服务端主动推通知
```
旧版跟 stdio/websocket 一样是一根管子混着来，必须 read loop + id 分发。
Streamable HTTP 把 POST 响应改回正常 HTTP 一来一回，是协议层面的改进。

### 对当前项目的意义

我们只用 Streamable HTTP，只做 list_tools 和 call_tool（普通 JSON 一问一答）。
理论上可以直接 `await client.post(url, json=request)` 拿响应，不需要内存队列。
SDK 的整套 read/write + _receive_loop 架构对我们是"杀鸡用牛刀"。
但好处是：如果以后换传输方式（比如 stdio），代码不需要改。

---

## 35. Event Loop 主循环结构：执行阶段 vs 等待阶段

### 核心伪代码

```python
while True:
    # 阶段1：执行 — 把 ready_queue 里的协程全部跑完
    while ready_queue:
        coroutine = ready_queue.popleft()
        coroutine.step()  # 执行到下一个 await 就停

    # 阶段2：等待 — ready_queue 空了，所有协程都在 await
    events = select(all_fds, timeout=nearest_timer)

    # 阶段3：唤醒 — select 返回，把就绪的 fd 对应的协程放回 ready_queue
    for fd in events:
        coroutine = fd_to_coroutine[fd]
        ready_queue.append(coroutine)
```

### 关键结论

1. **select 只在 ready_queue 清空后才被调用** — 所有能跑的协程都跑完了，没活干了，才去问内核
2. **协程切换（await）和 IO 检测（select）是两个独立阶段，不会交叉**
3. 协程在 await 处挂起时，只是把自己从 ready_queue 移出，不会触发 select
4. 这是**轮询模型**，不是中断模型：执行 → 等待 → 唤醒 → 循环

### 为什么协程不能有 CPU 密集操作

如果某个协程在 `step()` 里算了 10 秒才碰到下一个 `await`，这 10 秒内 select 不会被调用。
内核那边数据照样到达、fd 照样被标记为 readable，但 event loop 没机会调 select 去发现。
所有其他协程全部卡住 — 这就是"协程阻塞了 event loop"。

---

## 36. IO 的 set_result 全部发生在 select 之后

### 注册阶段（协程执行到 await 时）

```python
# 协程执行到 await sock.recv() 时，底层做的事：
fut = loop.create_future()
loop.add_reader(sock.fileno(), callback, fut)  # 把 fd 注册到 select 监听列表
await fut  # 协程挂起
```

`add_reader` 告诉 event loop："监听这个 fd，可读时调这个 callback"。

### 唤醒阶段（select 返回后）

```python
# select 说 sock 的 fd 可读了
data = sock.recv(4096)       # 真正读数据
fut.set_result(data)         # 设置 future 结果 → 协程被唤醒
```

### 完整链路

```
select 返回 fd 就绪
  → event loop 查表找到对应的回调
    → 回调内部 set_result(data)
      → future 有结果了
        → await 这个 future 的协程被放回 ready_queue
```

**所有 IO 相关的 set_result 都发生在 select 返回之后**，由 event loop 在主线程里顺序执行。
不存在某个后台线程偷偷 set 的情况。

唯一的例外是 `call_soon_threadsafe`，它通过往 self-pipe 写一个字节让 select 返回，
event loop 才有机会处理跨线程塞进来的回调。

---

## 37. asyncio.sleep 是"最少等待 N 秒"

### 机制

`asyncio.sleep(n)` 不是注册 fd，而是注册一个定时器。
event loop 在调 select 时把最近的定时器时间作为 timeout：

```python
nearest_timer = 3.0  # 比如 sleep(3) 注册的
events = select(all_fds, timeout=nearest_timer)
# select 最多等 3 秒就返回，即使没有 fd 就绪
```

### 延迟来源

**1. select 前的延迟**：如果 ready_queue 里有协程在跑，跑了 0.5 秒才跑完，select 调用就晚了 0.5 秒。

**2. select 后的排队**：select 返回时可能同时有多个事件就绪（3 个 socket 可读 + 1 个定时器到期），
event loop 把它们全部放进 ready_queue 按顺序执行。sleep 的回调排在后面就要多等。

```
实际等待 = N秒 + select前的执行耗时 + select后排在前面的协程耗时
```

一个协程卡 2 秒，所有 `asyncio.sleep` 都至少多等 2 秒。

---

## 38. 跨线程设置 Future 的正确方式

### 问题

从另一个线程直接调 `fut.set_result()` 不会唤醒 event loop。
因为 event loop 可能正卡在 select 上等 fd 就绪，它不知道有 future 被设置了。

### 错误方式

```python
def other_thread():
    time.sleep(1)
    fut.set_result("hello")  # 直接设置 — event loop 不知道！
```

event loop 卡在 select，没有 fd 就绪，不会返回，协程永远不被唤醒。

### 正确方式

```python
def other_thread():
    time.sleep(1)
    loop.call_soon_threadsafe(fut.set_result, "hello")
    # 内部流程：
    # 1. 把 fut.set_result("hello") 塞进回调队列
    # 2. 往 self-pipe 写一个字节
    # 3. select 检测到 self-pipe 可读 → 立即返回
    # 4. event loop 执行回调 → future 有结果 → 协程被唤醒
```

### 验证

`demo_threadsafe.py` 实测：
- 错误方式：event loop 卡在 select 5 秒不动，直到 force stop
- 正确方式：1 秒后立即唤醒，协程正常拿到结果
