# My Agent — 架构设计文档

基于 Claude Code 源码架构的 Agent 系统。
参考源码路径: D:\my_object\open-claude-code

## 技术栈
- Python 3.12+
- anthropic SDK
- 模型: claude-sonnet-4-6

## 代码质量需求
- 从一开始就用正确的文件结构，每个模块独立文件
- 已确定的设计（ToolResult、Tool注册表、agent loop）直接按最终结构写,在确定的部分要符合Claude code的结构进行设计，不可用省略
- 未确定的部分先写简单版，留好接口，后面迭代加字段
- 不过度设计：不加用不到的抽象，不写用不到的功能

## 环境变量
- ANTHROPIC_AUTH_TOKEN: API 密钥
- ANTHROPIC_BASE_URL: API 代理地址（可选）

## 设计需求
- 每完成一步要详细的介绍文件分类，以及设计原因
- 每次当用户进行需求提问或者请求增加的时候，可以结合Claude code源码架构进行分析用户需求和设计是否合理

## 进度管理
- docs/task_progress.md — 当前任务进度 + 新需求记录（合并一个文件，避免分散）
- CLAUDE.md 本身就是架构文档，不再另建 architecture.md
---

## 文件结构

```
src/
├── __init__.py
├── config.py          — 环境变量 + 全局配置常量
├── types.py           — ToolResult, ToolUseContext, AgentState, ToolDef, MessageHistory
├── events.py          — AgentEvent dataclass 层级（事件系统）
├── errors.py          — AgentErrorCode 枚举 + API 错误分类 + synthetic 消息
├── api.py             — Anthropic SDK 封装（query_model + create_stream_with_retry + side_query）
├── messages.py        — 消息构建辅助（tool schemas, tool_result, thinking 清洗）
├── system_prompt.py   — 系统提示构建（含记忆行为指令）
├── display.py         — 终端展示层（consume_events + default_handler）
├── tool_runner.py     — 统一工具执行入口（generator，支持事件冒泡）
├── agent_loop.py      — 主循环（run_agent_loop generator + agent_loop 顶层入口 + 记忆注入/提取）
├── main.py            — CLI REPL 入口
├── frontmatter.py     — YAML frontmatter 解析（skills/agents/memory 共用）
├── memory/
│   ├── __init__.py    — 公共 API
│   ├── paths.py       — 记忆目录路径解析 + 存在性检查
│   ├── scan.py        — 扫描记忆文件、构建 manifest（复用 frontmatter.py）
│   ├── prompt.py      — 构建行为指令（告诉 LLM 如何保存/读取记忆）
│   ├── recall.py      — Side query 选择相关记忆（findRelevantMemories）
│   └── extract.py     — 后台记忆提取（forked agent）
├── skills/
│   └── __init__.py    — Skill 发现/加载/缓存/reminder
├── agents/
│   ├── __init__.py    — Agent 发现/加载/缓存/reminder
│   └── explore.py     — 内置 Explore agent 定义
└── tools/
    ├── __init__.py    — ALL_TOOLS 注册表
    ├── bash.py        — shell 命令执行
    ├── read_file.py   — 文件读取
    ├── grep.py        — 内容搜索
    ├── skill.py       — Skill 工具（inline + fork 模式）
    └── agent.py       — Subagent 工具
```

---

## 1. ToolResult 统一返回协议

对应源码: `src/Tool.ts:149-156`

所有工具的 executor 返回统一的 ToolResult。agent loop 只处理这三个字段，不关心是哪个工具。

```python
@dataclass
class ToolResult:
    data: Any                              # 必须 — 结构化数据（给程序/map_result 用）
    new_messages: list[dict] = []          # 可选 — 额外注入的消息
    context_modifier: Callable | None      # 可选 — 修改后续上下文的回调
```

- `data`: 每个工具自定义格式，不统一。由 `map_result` 转成 LLM 可读文本
- `new_messages`: 大多数工具为空。Skill inline 模式用来注入 skill 内容
- `context_modifier`: 大多数工具为 None。Skill 用来限制后续可用工具/切换模型

---

## 2. Tool 注册表

对应源码: `src/tools/` 每个工具一个目录

每个工具通过 ToolDef 注册:

```python
@dataclass
class ToolDef:
    schema: dict                                          # API schema，发给 LLM 看的工具描述
    executor: Callable[[dict, ToolUseContext], ToolResult | Generator]  # 执行器
    map_result: Callable[[Any], str]                      # data → LLM 可读文本
    is_read_only: Callable[[dict], bool] | None = None    # 可选，判断是否只读操作

ALL_TOOLS: dict[str, ToolDef] = { "bash": ..., "read_file": ..., ... }
```

executor 有两种形态:
- 普通 executor（bash, read_file, grep）: 直接 `return ToolResult(...)`
- Generator executor（agent, fork skill）: `yield AgentEvent` + `return ToolResult(...)`

tool_runner.py 通过 `hasattr(result_or_gen, '__next__')` 自动区分。

**数据流:**
```
LLM 调用工具 → executor(inputs, ctx) → ToolResult { data, new_messages, context_modifier }
                                            │
                         map_result(data) ───┘→ LLM 可读文本 → 拼入 tool_result 消息
```

**为什么 executor 和 map_result 分开:**
data 不只给 LLM 用。Claude Code 里同一个 data 被三个消费者使用:
- map_result → 给 LLM 读的文本
- renderToolResultMessage → 给终端 UI 展示
- extractSearchText → 给搜索索引

我们目前只有 LLM 一个消费者，但保持分离为将来扩展留空间。

---

## 3. 基础工具

### bash
```python
# executor 返回:
data = {"stdout": "...", "stderr": "...", "interrupted": False}

# map_result 转换后 LLM 看到:
"ls -la\nfile1.txt\nfile2.txt"
```

### read_file
```python
# executor 返回:
data = {"type": "text", "content": "1\timport os\n2\timport sys\n...", "total_lines": 42}

# map_result 转换后 LLM 看到:
"1\timport os\n2\timport sys\n..."
```

### grep
```python
# executor 返回:
data = {"num_files": 3, "num_matches": 7, "content": "foo.py:10: matched line\n..."}

# map_result 转换后 LLM 看到:
"foo.py:10: matched line\nbar.py:22: another match\n..."
# 如果无匹配: "No matches found"
```

---

## 4. Skill 工具

对应源码: `src/tools/SkillTool/SkillTool.ts`

Skill 是注册在 ALL_TOOLS 里的普通工具。它的特殊之处仅在于: 返回的 ToolResult 里填了 new_messages 和 context_modifier。

### Skill 实例结构
```python
{
    "name": "project-analyzer",
    "description": "分析项目架构",
    "isfork": False,
    "allow_tools": ["bash", "read_file"],  # 可选白名单，为空表示不限制
    "body": "SKILL.md 的正文内容（不含 frontmatter）"
}
```

约束: 只有 allow_tools 白名单，没有黑名单。

### Skill 加载流程
1. `discover_skills()` — 扫描 skills/ 目录，解析每个 SKILL.md 的 frontmatter
2. `build_skill_content(skill_info)` — 拼接正文 + 替换 ${SKILL_DIR} 变量
3. Skill 列表通过 `<system-reminder>` 注入到用户消息中，让 LLM 知道有哪些 skill 可用

### inline 模式（isfork=False）的 execute 返回值
```python
ToolResult(
    data={"success": True, "commandName": "project-analyzer", "status": "inline"},
    new_messages=[{
        "role": "user",
        "content": "<skill-content name='project-analyzer'>\n{body}\n</skill-content>\n\n请按照技能指南执行。"
    }],
    context_modifier=lambda ctx: {**ctx, "allowed_tools": ["bash", "read_file"]}
)
```
map_result 转换后 LLM 看到: `"Launching skill: project-analyzer"`
然后 agent loop 注入 new_messages，LLM 紧接着收到一条包含 skill 完整指导的 user message。

### fork 模式（isfork=True）的 execute 返回值
```python
# _execute 是 generator executor — yield 事件，return ToolResult
def _execute(inputs, context):
    ...
    if skill.is_fork:
        return (yield from _execute_fork(skill, inputs, context))
    return _execute_inline(skill, inputs, context)

def _execute_fork(skill, inputs, context):
    gen = run_agent_loop(messages=..., system_prompt=..., tools=..., ...)
    result_text = yield from gen  # 子 agent 事件冒泡到父层
    return ToolResult(
        data={"success": True, "commandName": "...", "status": "forked", "result": result_text}
        # 没有 new_messages，没有 context_modifier
    )
```
map_result 转换后 LLM 看到: `'Skill "project-analyzer" completed (forked).\n\nResult:\n...'`

---

## 5. Subagent 工具

对应源码: `src/tools/AgentTool/`

Subagent 也是 ALL_TOOLS 里的普通工具。executor 内部启动一个新的 agent loop。

### Agent 实例结构
```python
{
    "name": "Explore",
    "description": "快速搜索代码库",
    "system_prompt": "You are a code exploration agent...",
    "max_turns": 12,
    "allowed_tools": ["bash", "read_file", "grep"],  # 可选工具白名单（None=全部）
    "disallowed_tools": ["Skill"],                    # 可选工具黑名单（applied after whitelist）
    "skills": ["commit", "review-pr"],                # 可选 skill 实例白名单（None=全部）
}
```

### execute 返回值
```python
# _execute 是 generator executor — yield 事件，return ToolResult
def _execute(inputs, context):
    ...
    gen = run_agent_loop(messages=..., system_prompt=..., tools=..., ...)
    result_text = yield from gen  # 子 agent 事件冒泡到父层
    return ToolResult(
        data={"agent_name": "Explore", "result": result_text}
        # 没有 new_messages，没有 context_modifier
    )
```
map_result 转换后 LLM 看到: 子 agent 的最终输出文本

### 约束
- 子 agent 的 system_prompt 里写明: "Do NOT spawn sub-agents; execute directly."
- depth 参数做兜底限制，超过则报错
- 子 agent 不能派生新的子 agent（通过 prompt + depth 双重控制）

### 两层过滤模型

Tool 和 Skill 使用不同层级的过滤:

**Layer 1 — Tool 大类过滤**（bash, read_file, grep, Skill, agent）
- `allowed_tools`: 白名单。`None` = 全部工具（默认）
- `disallowed_tools`: 黑名单。应用在白名单之后
- `_resolve_tools()` 执行: whitelist → exclude 'agent' → subtract blacklist
- `build_tool_schemas()` 按最终工具列表生成 schema，不再强制包含 Skill

**Layer 2 — Skill 实例过滤**（commit, review-pr, pdf 等具体 skill）
- `skills`: 精确模式白名单。`None` = 全部 skill（默认）
- 控制 `format_skill_listing()` 的 `filter_names` 参数
- 只影响 `<system-reminder>` 中注入的 skill 列表，不影响 Skill tool 本身
- 如果 Skill 工具被 disallowed_tools 移除，skill 列表也不会注入（无意义）

---

## 6. Agent Loop 主循环

对应源码: `src/query.ts` + `src/services/tools/toolExecution.ts`

### 两层入口

- `agent_loop()` — 顶层 REPL 入口: 管理 MessageHistory，注入 skill/agent reminder，委托 run_agent_loop()
- `run_agent_loop()` — 低层 generator: 接受原始 messages + config，运行 LLM ↔ tool 循环，yield AgentEvent，return str

```
用户输入
  ↓
agent_loop(): 注入 <system-reminder> skill/agent 列表
  ↓
run_agent_loop() [generator]:
  ┌──→ 调 LLM API（stream 模式: yield TextDelta 逐 token）
  │      ↓
  │    stop_reason == end_turn? ──是──→ yield TokenUsage → return 最终文本
  │      ↓ 否（tool_use）
  │    遍历所有 tool_use blocks:
  │      ├── yield ToolStart
  │      ├── result, llm_text, is_error = yield from run_tool_use(...)
  │      │     ├── 普通 executor: 直接调用，返回 ToolResult
  │      │     └── generator executor: yield from 冒泡子 agent 事件
  │      ├── yield ToolEnd
  │      ├── 收集 tool_result 消息（llm_text + is_error）
  │      ├── 收集 new_messages（如果有）
  │      └── 应用 context_modifier（如果有）
  │      ↓
  │    拼接消息 + 下一轮
  └─────
  ↓
consume_events(gen, default_handler): 迭代事件 → 打印到终端 → 返回最终文本
```

### run_tool_use 统一入口（generator）
对应源码: `toolExecution.ts:1207 runToolUse()`

```python
def run_tool_use(tool_name, tool_input, tool_use_id, context
) -> Generator[AgentEvent, None, tuple[ToolResult, str, bool]]:
    tool = ALL_TOOLS[tool_name]
    result_or_gen = tool.executor(tool_input, context)
    if hasattr(result_or_gen, '__next__'):
        result = yield from result_or_gen  # generator executor — 冒泡事件
    else:
        result = result_or_gen             # 普通 executor
    llm_text = tool.map_result(result.data)
    return result, llm_text, False
```

### 真流式

stream 模式下，agent_loop 直接迭代 SDK 的 text_stream，逐 token yield TextDelta:

```python
def _stream_call(history, system_prompt, tools, thinking, on_retry, label):
    stream_cm = create_stream_with_retry(messages=..., system=..., tools=..., ...)
    with stream_cm as api_stream:
        for text in api_stream.text_stream:
            yield TextDelta(label=label, delta=text, first=_first)
        return api_stream.get_final_message()
```

`with` 管理 HTTP 长连接的生命周期（进入时建立连接，退出时关闭 socket）。

---

## 7. ToolUseContext 与 AgentState 分层

### ToolUseContext（传给工具的执行环境）
```python
@dataclass
class ToolUseContext:
    messages: list          # 当前消息列表
    tools: list[str]        # 当前可用工具名称列表
    depth: int              # 当前嵌套深度（限制子agent递归）
    abort_signal: bool      # 是否应该中止执行
```

### AgentState（agent loop 维护的统计信息）
```python
@dataclass
class AgentState:
    agent_id: str                # 当前 agent 标识
    total_input_tokens: int      # 累计输入 token
    total_output_tokens: int     # 累计输出 token
    total_thinking_tokens: int   # 累计 thinking token（extended thinking 模式）
```

工具执行时通过 ToolUseContext 获取环境信息，不接触 AgentState。
AgentState 由 agent loop 在外层维护和更新。

---

## 8. 消息拼接规则

### 消息结构
```python
{"role": "user" | "assistant", "content": str | list}
```

### 拼接顺序（每轮 tool_use 后）
```
1. assistant 消息: response.content（包含 text block + tool_use blocks）
2. user 消息:      [{"type": "tool_result", "tool_use_id": "...", "content": llm_text}, ...]
3. 如果有 new_messages（Skill inline 产生的）:
   3a. assistant 占位: {"role": "assistant", "content": [{"type": "text", "text": "I've loaded the skill..."}]}
   3b. user 消息:      new_messages 里的内容（skill-content）
```

为什么需要 3a 占位: Claude API 要求 user/assistant 严格交替。第 2 步是 user，第 3b 步也是 user，中间必须插一条 assistant。

### 约束
- 消息列表只增不减（不 clear）
- 上下文过长时做 compaction（压缩摘要），不是清空
- 上下文重置（如用户主动清除）在 agent loop 层处理，重置所有状态，不只是消息

---

## 9. 事件系统

对应源码: Claude Code 的 Message yield 模式

核心逻辑（agent_loop, tool_runner, api）不做任何 print()。所有输出通过 yield AgentEvent 对象，由外层消费者决定展示方式。

### 事件类型
```python
AgentEvent(label)              # 基类，label 标识来源（"main", "agent:Explore", "fork:commit"）
├── TextDelta(delta, first)    # 流式文本片段（逐 token）
├── TextBlock(text)            # 完整文本块（非流式模式）
├── ThinkingBlock(thinking)    # Extended thinking 内容
├── ToolStart(tool_name, tool_input)   # 工具开始执行
├── ToolEnd(tool_name, is_error, result_summary)  # 工具执行完成
├── ErrorEvent(error_text)     # API 错误
├── Recovery(message)          # 错误恢复（如 thinking-400 重试）
├── TokenUsage(input_tokens, output_tokens, thinking_tokens)  # token 统计
└── RetryNotice(delay, attempt, max_attempts)  # API 重试通知
```

### 消费模式
```python
# 主 agent — 打印到终端
result = consume_events(gen, default_handler)

# 子 agent — 事件通过 yield from 冒泡到父层
result_text = yield from gen
```

### 事件冒泡链
```
run_agent_loop (主)
  └── yield from run_tool_use
        └── yield from tool.executor (generator executor)
              └── yield from run_agent_loop (子 agent)
                    └── yield TextDelta / ToolStart / ToolEnd / ...
```

每层 `yield from` 把子 generator 的事件透传给父层，最终到达 consume_events()。

---

## 10. 错误处理

### API 错误分类
```python
class AgentErrorCode(Enum):
    API_CONNECTION_ERROR    # 连接失败
    API_TIMEOUT             # 超时
    API_RATE_LIMIT          # 429
    API_OVERLOADED          # 529
    API_AUTH_ERROR           # 401/403
    API_BAD_REQUEST          # 400
    API_SERVER_ERROR         # 500/502
    API_UNKNOWN              # 其他
    TOOL_EXECUTOR_ERROR      # 工具执行异常
    TOOL_MAP_RESULT_ERROR    # map_result 异常
    TOOL_NOT_FOUND           # 未知工具
```

### 错误恢复策略
- API 错误 → 注入 synthetic assistant 消息（保持消息交替）→ 返回错误文本
- Thinking 400 → strip thinking blocks → 重试一次
- 工具 executor 异常 → is_error=True + `<tool_use_error>` 包裹
- 孤儿 tool_use（tool_use 无对应 tool_result）→ 自动补全 is_error=True 占位

---

## 11. Extended Thinking

- `config.THINKING_ENABLED` / `config.THINKING_BUDGET_TOKENS` 控制
- thinking 块的 signature 必须原样保留发回 API
- redacted_thinking 块: API 审查过的不透明数据，必须原样回传
- 400 错误恢复: 检测 "invalid signature" / "thinking blocks cannot be modified" → strip + filter → 重试

---

## 开发顺序

1. 项目骨架 + ToolResult 数据结构
2. 基础工具（bash, read_file, grep）+ map_result + ALL_TOOLS 注册表
3. Agent Loop 主循环（跑通普通工具）
4. Skill 系统（inline 模式）
5. Skill fork 模式
6. Subagent 工具

每完成一步都运行测试，确认能跑通后再继续下一步。
