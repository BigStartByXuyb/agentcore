# My Agent 架构重构总结 — 消息系统设计

## 概述

基于 Claude Code 源码架构的 Python Agent 系统，完成了消息系统从 raw dict 到类型安全的 dataclass 架构重构。

---

## 1. 两层消息模型

### Message 层（单条消息）

```python
@dataclass
class Message:
    role: str                           # "user" | "assistant"
    content: str | list[ContentBlock]   # 文本或结构化内容块列表
    msg_type: MessageType               # "human" | "assistant" | "tool_result" | "meta"
    attachments: list[Attachment]       # 附加数据（reminder、memory等）
    timestamp: float

    def attach(self, attachments: list[Attachment]) -> None:
        self.attachments.extend(attachments)
```

`msg_type` 区分同一 API role 下的不同语义：
- `"human"`: 真实用户输入
- `"tool_result"`: 工具执行结果（API role 是 "user"）
- `"meta"`: 系统注入内容（skill 列表等）
- `"assistant"`: LLM 回复

### ContentBlock 层（消息内容的组成单元）

```python
ContentBlock = TextContent | ToolUseContent | ToolResultContent | ThinkingContent | RedactedThinkingContent
```

每种 block 是独立 dataclass：
- `TextContent(text)` — 纯文本
- `ToolUseContent(id, name, input)` — LLM 发起的工具调用
- `ToolResultContent(tool_use_id, content, is_error)` — 工具执行结果
- `ThinkingContent(thinking, signature)` — Extended thinking
- `RedactedThinkingContent(data)` — 审查后的不透明 thinking

**关键约束**：Message 层和 ContentBlock 层永远不交叉。一条 Message 的 content 是 `str` 或 `list[ContentBlock]`，不会出现嵌套 Message。

---

## 2. Attachment 系统

```python
@dataclass
class Attachment:
    type: AttachmentType    # "relevant_memories" | "system_reminder"
    content: str            # 文本内容
    metadata: dict          # 额外元数据（如记忆文件路径列表）
```

### 设计意图

Attachment 是**挂在 Message 上的补充信息**，不直接发送给 API。`normalized_for_api()` 展开时把它们作为额外的 text block 追加到消息 content 里。

### 适用场景

- `system_reminder`: skill/agent 列表提醒
- `relevant_memories`: 记忆上下文注入

### attach vs inject_messages 的区别

| | attach | inject_messages |
|---|---|---|
| 作用 | 把数据挂到现有消息上 | 在 history 末尾追加新的独立消息 |
| 展开方式 | normalized_for_api() 合并进宿主消息的 content | 作为独立 turn 存在 |
| 语义 | 背景信息，不需要 LLM 专门回应 | 新指令，需要 LLM 当作新 turn 处理 |
| 使用者 | reminder、memory | Skill inline 的 new_messages |

---

## 3. MessageHistory — 会话状态管理器

```python
class MessageHistory:
    def __init__(self, messages: list[Message] | None = None) -> None:
        self._messages = messages if messages is not None else []

    # 写入
    def add_user(content, msg_type="human") -> Message
    def add_assistant(content: list[ContentBlock]) -> Message
    def add_tool_results(blocks: list[ToolResultContent]) -> Message
    def add_assistant_placeholder(text="I've loaded...") -> Message
    def inject_messages(new_messages: list[Message]) -> None

    # 读取
    def normalized_for_api() -> list[dict]   # 转换为 API 格式
    def last_user_message() -> Message | None
```

### normalized_for_api() 三步转换

1. ContentBlock → dict（API 格式）
2. 展开 attachments 为额外 text block
3. 合并连续同 role 消息（API 要求严格交替）

### 为什么需要合并

主 agent 的 reminder/memory 通过 attach 挂在 user message 上，不会产生连续同 role。但子 agent 的 inject_messages 可能产生连续 user 消息，merge 逻辑兜底处理。

---

## 4. ToolUseContext — 工具执行环境

```python
EventCallback = Callable[["AgentEvent"], None]

@dataclass
class ToolUseContext:
    messages: MessageHistory    # 当前会话历史（单一数据源）
    tools: list[str]            # 可用工具名称列表
    depth: int = 0              # 嵌套深度
    abort_signal: bool = False
    tool_overrides: dict | None = None
    permissions: Any | None = None      # PermissionEngine 实例
    on_event: EventCallback = lambda ev: None  # 事件回调（子agent/工具用来发送事件）
```

**关键设计决策**：
- `messages` 字段直接持有 `MessageHistory`，是 `run_agent_loop` 操作消息的唯一入口
- `on_event` 让工具执行器（skill fork、subagent）能向上层发送事件，无需 generator 冒泡

---

## 5. Agent Loop 架构

### 两层入口

- `agent_loop()` — 顶层 REPL 入口：管理 history、注入 reminder/memory、委托 run_agent_loop
- `run_agent_loop()` — 低层循环：从 `tool_use_context.messages` 取 history，运行 LLM ↔ tool 循环

### run_agent_loop 签名

```python
async def run_agent_loop(
    *,
    memory_task: asyncio.Task[None] | None = None,
    system_prompt: str,
    tool_use_context: ToolUseContext,    # history 在这里面
    max_turns: int,
    state: AgentState | None = None,
    label: str = "main",
    stream: bool = False,
    thinking: dict | None = None,
    on_event: EventCallback,            # 回调函数，替代 generator 事件冒泡
) -> str                                # 直接返回最终文本，不再是 AsyncGenWithResult
```

**重构说明**：原来使用 `AsyncGenWithResult[AgentEvent, str]`，通过 `yield` 冒泡事件、`set_result()` 设置返回值。现在改为 `on_event` 回调 + 直接 `return`，删除了 `AsyncGenWithResult`。

### 主 agent 调用路径

```python
async def agent_loop(user_input, history, state):
    user_msg = history.add_user(user_input)

    # Reminder 作为 attachment 挂到 user message
    reminders = build_metadata_reminders(main_tools, use_sent_tracking=True)
    if reminders:
        user_msg.attach(reminders)

    # Memory 异步注入（不阻塞）
    memory_task = asyncio.create_task(_prepare_memory_context(user_input, user_msg))

    # 构建交互式事件处理器（拦截 PermissionRequest 进行用户交互）
    handler = make_interactive_handler(default_handler)

    # ToolUseContext 持有 history
    tool_use_context = ToolUseContext(messages=history, tools=...)

    # 直接 await，不再需要 consume_events()
    result = await run_agent_loop(
        memory_task=memory_task,
        system_prompt=system,
        tool_use_context=tool_use_context,
        on_event=handler,
        ...
    )

    # 后台记忆提取（fire-and-forget，事件静默丢弃）
    asyncio.create_task(run_memory_extraction(..., on_event=lambda _: None))
```

### 子 agent 调用路径（agent.py / skill.py / extract.py）

```python
# 构建独立的 MessageHistory
initial_messages = [Message(role="user", content=prompt, msg_type="human")]
sub_history = MessageHistory(initial_messages)

# Reminder 挂到 user message
msg = sub_history.last_user_message()
if msg is not None:
    msg.attach(build_metadata_reminders(...))

# ToolUseContext 持有子 agent 自己的 history + on_event 回调
sub_context = ToolUseContext(
    messages=sub_history,
    tools=sub_tool_names,
    depth=context.depth+1,
    on_event=context.on_event,   # 透传父层的事件回调
)

# 直接 await，事件通过 on_event 回调传递给父层
result_text = await run_agent_loop(
    system_prompt=agent_def.system_prompt,
    tool_use_context=sub_context,
    on_event=context.on_event,   # 同样透传
    ...
)
```

---

## 6. Tool 执行后的消息拼接

每轮 tool_use 后的消息序列：

```
1. assistant 消息: [text blocks + tool_use blocks]     ← history.add_assistant()
2. user 消息:      [tool_result blocks]                ← history.add_tool_results()
3. 如果有 new_messages（Skill inline 产生的）:
   3a. assistant: "I've loaded the requested content." ← history.add_assistant_placeholder()
   3b. user:      skill 指令内容                        ← history.inject_messages()
```

**为什么需要 3a 占位**：第 2 步是 user，第 3b 步也是 user，API 要求严格交替，中间必须插一条 assistant。

**为什么 skill 指令不用 attach**：attach 是背景信息，合并进宿主消息。skill 指令是新 turn 的指令，需要 LLM 在 assistant 确认后才看到，才会认真执行。

---

## 7. 记忆系统

### 异步注入（不阻塞主循环）

```python
memory_task = asyncio.create_task(_prepare_memory_context(user_input, user_msg))
```

`_prepare_memory_context` 异步执行：
1. 读取 MEMORY.md 索引
2. 用 side query 找相关记忆
3. 读取记忆文件内容
4. 通过 `user_msg.attach(...)` 挂到 user message 上

`run_agent_loop` 每轮检查 `memory_task.done()`，如果完成则获取结果（处理异常）。

### 后台记忆提取

每轮结束后，`run_memory_extraction` 作为 fire-and-forget task 运行：
- 深拷贝当前 messages
- 启动独立的 extraction agent（受限 bash 工具，只能写 memory 目录）
- 审查对话，提取值得保存的记忆

---

## 8. build_metadata_reminders 返回 Attachment

```python
def build_metadata_reminders(tools, ...) -> list[Attachment]:
    # 返回 0-2 个 Attachment（skill listing + agent listing）
```

调用方直接 `msg.attach(reminders)` 挂到 user message 上。

底层的 `build_skill_reminder()` 和 `build_agent_reminder()` 也返回 `Attachment | None`：

```python
def build_skill_reminder() -> Attachment | None:
    return Attachment(
        type="system_reminder",
        content=f"<system-reminder>\n{listing}\n</system-reminder>",
    )
```

---

## 9. API 边界

| 层级 | 类型 | 用途 |
|---|---|---|
| 内部 | `Message` + `list[ContentBlock]` + `Attachment` | 类型安全、语义清晰 |
| API | `list[dict]` | Anthropic SDK 要求的格式 |

转换发生在 `normalized_for_api()` 一个点，其他地方全部用类型安全的对象。

---

## 10. 关键设计决策总结

1. **ToolUseContext 持有 MessageHistory** — 单一数据源，run_agent_loop 不需要额外的 history 参数
2. **attach 在 Message 上** — 不在 MessageHistory 上，因为操作的是消息本身
3. **Reminder/Memory 用 attach** — 背景信息，合并进 user message
4. **Skill new_messages 用 inject_messages** — 新指令，需要独立 turn + assistant 占位
5. **子 agent 构建独立 MessageHistory** — 通过 `MessageHistory(initial_messages)` 创建
6. **Memory 异步注入** — `asyncio.create_task`，不阻塞主循环
7. **ContentBlock 是 provider-agnostic** — 为未来多 LLM 支持预留
8. **on_event 回调替代 generator 冒泡** — 所有事件通过 `on_event(event)` 同步回调传递，删除了 `AsyncGenWithResult`
9. **on_event 是同步回调** — 99% 事件只需 print；PermissionRequest 特殊处理：callback 内用 `asyncio.ensure_future()` 调度异步 input，`await future` 在 tool_runner 中自然等待
10. **on_event 传递方式** — 内部函数用显式参数，工具 executor 通过 `ToolUseContext.on_event`
11. **并发工具事件顺序保证** — 每个并发 task 用独立 Queue 缓冲事件，drain 时按工具顺序消费
