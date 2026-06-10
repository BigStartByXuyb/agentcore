# Agentcore

基于Python 设计的Agent 系统，支持多工具调用、Skill 系统、Subagent 派发、mcp系统(包括被动接受工具列表变更信息，网络错误重连信息)，记忆持久化、上下文压缩、权限管理、skill,subagent,mcp,setting文件热重载、信息异常处理，错误恢复等核心能力。

## 功能特性

- 多工具系统 — 内置 Bash、文件读写、Grep、Glob 等工具，支持自定义扩展
- Skill 系统 — 支持 inline / fork 两种模式，可加载外部 Skill 指导 Agent 行为,skill文件和claudecode/codex等skill文件格式相同
- Subagent 派发 — 支持 Explore、Plan 等子 Agent，可并行处理独立子任务，Agent文件和claudecode/codex等Agent文件格式相同
- 记忆系统 — 自动提取和召回跨会话记忆，支持 side-query 相关性筛选
- 上下文压缩 — 多层压缩策略（micro / auto compact），防止上下文溢出
- 会话持久化 — 会话保存在本地，下次打开可继续上次对话
- 多模型适配 — Provider 抽象层，支持 Anthropic / DeepSeek 等多个模型提供商
- MCP 工具集成 — 支持通过 MCP 协议接入外部工具服务
- 权限控制 — 工具级别的权限管理，支持白名单/黑名单
- 配置热重载 — settings.json 变更后自动生效，无需重启

### 项目核心

架构轻量，扩展性预留充足，有着完整的agent生命周期管理，架构内部流程清晰，沉余部分较少，可以直接使用claude/codex等大厂的skill，agent文件进行能力扩展，生态可移植性和兼容性较强

### 环境要求

- Python 3.12+

### 安装

```bash
git clone https://github.com/BigStartByXuyb/agentcore.git
cd my-agent
pip install -e .
```

### 配置

设置环境变量（API 密钥至少配置一个提供商的即可）：

| 变量名 | 说明 |
|---|---|
| `ANTHROPIC_AUTH_TOKEN` | Anthropic API 密钥 |
| `ANTHROPIC_BASE_URL` | Anthropic API 代理地址（可选） |
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 |
| `DEEPSEEK_BASE_URL` | DeepSeek API 代理地址（可选） |

> 环境变量仅用于 API 凭证，其他所有配置均通过 `settings.json` 管理。

### Settings 配置文件

首次启动时会自动在项目目录下生成 `.my-agent/settings.json`，继承全局配置。同一台机器多个项目可以各自独立配置。

配置优先级：代码默认值 → 全局 `~/.my-agent/settings.json` → 项目 `.my-agent/settings.json` → 环境变量

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `provider` | `"anthropic"` | 模型提供商（`anthropic` / `deepseek`） |
| `model` | `"claude-sonnet-4-6"` | 主模型名称 |
| `compact_model` | null | 上下文压缩用模型（null = 跟随主模型） |
| `side_query_model` | null | 记忆召回等轻量查询用模型 |
| `fallback_model` | null | 主模型失败时的降级模型 |
| `max_tokens` | `16384` | 单次 LLM 回复的最大 token 数 |
| `max_context_window` | `200000` | 上下文窗口大小（token），超过触发压缩 |
| `max_turns` | `30` | 单次对话最大轮次（防止无限循环） |
| `max_agent_depth` | `3` | 子 Agent 最大嵌套深度 |
| `thinking_enabled` | `true` | Extended Thinking 开关 |
| `thinking_budget_tokens` | `10000` | Thinking 预算 token 数（必须 < max_tokens） |
| `memory_enabled` | `true` | 跨会话记忆系统开关 |
| `memory_max_files` | `200` | 记忆目录最大文件扫描数 |
| `memory_max_relevant` | `5` | 每轮召回的最大相关记忆数 |
| `session_persist_enabled` | `true` | 会话持久化开关（保存到本地 JSON） |
| `micro_compact_enabled` | `true` | 微压缩开关（无损清理旧 tool_result） |
| `micro_compact_keep_recent` | `6` | 微压缩保留最近 N 轮完整内容 |
| `auto_compact_max_tokens` | `4096` | 自动压缩摘要的最大 token 数 |
| `sandbox_enabled` | `true` | 沙箱隔离开关（仅 Linux/WSL2 生效） |

### 运行

```bash
python -m src.main
```

## 使用示例

启动后进入交互式对话，直接输入问题即可：

```
You: 帮我看一下当前目录有哪些文件
Agent: [调用 bash 工具执行 ls] ...
```

内置斜杠命令：

| 命令 | 说明 |
|---|---|
| `/clear` | 清空当前上下文 |
| `/sessions` | 查看历史会话列表 |
| `/resume` | 恢复上次会话 |
| `/plan` | 进入计划模式 |
| `/settings` | 查看/修改配置 |

## 编程接口 (API)

除了 CLI 交互，项目暴露了 Python API，可以直接在你的代码中集成：

### 快速集成

```python
import asyncio
from src import init, agent_loop, MessageHistory, AgentState, make_headless_handler

async def main():
    await init()
    history = MessageHistory()
    state = AgentState()
    result = await agent_loop(
        "列出当前目录的文件",
        history, state,
        on_event=make_headless_handler(),
    )
    print(result.text)    # Agent 的最终输出
    print(result.ok)      # True = 正常完成

asyncio.run(main())
```

### 低层 API（自定义工具/系统提示/权限）

```python
from src import init, run_agent_loop, ToolUseContext, MessageHistory, make_allow_all_handler
from src.permissions import get_permission_engine

async def main():
    await init()
    history = MessageHistory()
    history.add_user("Hello")
    ctx = ToolUseContext(
        messages=history,
        tools=["bash", "read_file"],              # 只开放部分工具
        system_prompt="You are a code reviewer.",
        label="my-reviewer",
        permissions=get_permission_engine(),       # 加载权限规则
    )
    result = await run_agent_loop(
        tool_use_context=ctx,
        max_turns=10,
        on_event=make_allow_all_handler(),         # 自动批准所有权限请求
    )
    print(result.text)

asyncio.run(main())
```

### 核心函数

| 函数 | 说明 |
|---|---|
| `init()` | 一次性初始化（加载配置、MCP 等），`agent_loop()` 前调用一次 |
| `agent_loop(user_input, history, state)` | 高层入口 — 自动管理记忆、Skill/memery/subagent/filecache 注入、权限提示 |
| `run_agent_loop(tool_use_context, max_turns)` | 低层入口 — 调用方自行控制工具列表、系统提示 |

### 核心数据结构

| 类 | 说明 |
|---|---|
| `MessageHistory` | 会话消息管理器，维护完整的对话历史 |
| `AgentState` | Agent 运行时统计（token 用量、轮次等） |
| `LoopResult` | `run_agent_loop` 的返回值，包含 `reason`（完成原因）和 `text`（输出文本） |
| `ToolUseContext` | 工具执行环境 — 包含消息历史、可用工具列表、系统提示、嵌套深度等 |

### 事件处理器

通过 `on_event` 回调接收 Agent 运行过程中的实时事件：

**文本事件：**

| 事件类 | 说明 |
|---|---|
| `TextDelta` | 流式文本片段（逐 token），`first=True` 标记新回复的第一个 chunk |
| `TextBlock` | 完整文本块（非流式路径） |
| `ThinkingDelta` | 流式 Thinking 片段 |
| `ThinkingBlock` | 完整 Thinking 块 |

**工具事件：**

| 事件类 | 说明 |
|---|---|
| `ToolStart` | 工具开始执行，携带 `tool_name` 和 `tool_input` |
| `ToolEnd` | 工具执行完成，携带 `is_error`、`result_summary` |

**权限事件：**

| 事件类 | 说明 |
|---|---|
| `PermissionRequest` | 工具需要用户授权，消费者通过 `future` 返回批准/拒绝结果 |
| `PermissionDenied` | 工具被权限规则拒绝 |

**用户交互事件：**

| 事件类 | 说明 |
|---|---|
| `UserQuestionRequest` | LLM 向用户提出结构化问题，消费者通过 `future` 返回用户选择 |

**子 Agent 事件：**

| 事件类 | 说明 |
|---|---|
| `SubAgentStart` | 子 Agent / fork Skill 开始执行，携带 `depth`（嵌套深度） |
| `SubAgentEnd` | 子 Agent / fork Skill 执行完成 |

**上下文压缩事件：**

| 事件类 | 说明 |
|---|---|
| `CompactCircuitBreaker` | 压缩连续失败触发熔断 |
| `BlockingLimitReached` | 上下文硬上限到达，无法继续调用 API |

**状态事件：**

| 事件类 | 说明 |
|---|---|
| `TokenUsage` | Token 用量统计（input / output / thinking） |
| `ErrorEvent` | API 错误或异常 |
| `Recovery` | Thinking 相关 400 错误恢复尝试 |
| `RetryNotice` | API 重试通知（携带 delay、attempt、max_attempts） |

预置处理器：

| 处理器 | 说明 |
|---|---|
| `make_interactive_handler()` | 终端交互模式 — 权限弹窗、用户输入（CLI 默认） |
| `make_headless_handler()` | 无头模式 — 自动拒绝权限，跳过用户交互 |
| `make_allow_all_handler()` | 信任模式 — 自动批准所有权限（脚本/测试用） |

## 配置文件格式
注意...我们当前的skill,agent都是可以直接复用全局项目下.claude目录的skill/agent文件夹的，因为方便通用，不用过多移植这些项目文件，可以在代码中进行更改目录
### MCP 配置 (mcp.json)

通过 JSON 文件配置 MCP 工具服务器。支持 stdio 和 HTTP 两种连接方式。

**配置文件位置：**
- 全局：`~/.my-agent/mcp.json`
- 项目：`<项目目录>/.mcp.json`（优先级更高，同名覆盖全局）

**stdio 模式（本地进程）：**

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@anthropic-ai/mcp-server-filesystem", "/path/to/dir"],
      "env": {
        "NODE_ENV": "production"
      }
    }
  }
}
```

**HTTP 模式（远程服务）：**

```json
{
  "mcpServers": {
    "remote-tools": {
      "type": "streamable_http",
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `command` | stdio 模式必填 | 可执行文件路径 |
| `args` | 否 | 命令行参数列表 |
| `env` | 否 | 环境变量，支持 `${VAR}` 引用系统变量 |
| `type` | HTTP 模式必填 | 连接类型：`streamable_http` |
| `url` | HTTP 模式必填 | 服务端 URL |

---

### Skill 文件 (SKILL.md)

每个 Skill 一个子目录，目录内包含 `SKILL.md` 文件。格式与 Claude Code / Codex 的 Skill 文件兼容，可直接复用。

**搜索路径（按优先级）：**
1. `<项目>/skills/` — 项目级
2. `<项目>/.claude/skills/` — 兼容 Claude Code 项目结构
3. `~/.claude/skills/` — 全局，所有项目共享

**目录结构：**
```
skills/                       # 或 .claude/skills/
├── my-skill/
│   └── SKILL.md
└── another-skill/
    └── SKILL.md
```

**inline 模式（默认，在主对话中执行）：**

```markdown
---
description: 描述这个 Skill 的功能
when_to_use: 什么场景下触发这个 Skill
allowed-tools: read_file, grep
---

# Skill 正文

这里写 Skill 的指导内容，LLM 会按照这些指令执行。
支持 ${SKILL_DIR} 变量引用 Skill 所在目录。
```

**fork 模式（在独立子 agent 中执行）：**

```markdown
---
description: 在隔离环境中执行的 Skill
context: fork
allowed-tools: bash
---

# Fork Skill 正文

这个 Skill 会启动一个独立的子 agent 执行，不影响主对话上下文。
```

| frontmatter 字段 | 必填 | 说明 |
|---|---|---|
| `description` | 是 | Skill 功能描述，用于 LLM 判断是否调用 |
| `when_to_use` | 否 | 触发场景说明 |
| `context` | 否 | 执行模式：省略为 inline，`fork` 为独立子 agent |
| `allowed-tools` | 否 | 工具白名单（逗号分隔），省略表示不限制 |

---

### Agent 文件 (AGENT.md)

每个 Agent 一个子目录，目录内包含 `AGENT.md` 文件。格式与 Claude Code / Codex 的 Agent 文件兼容，可直接复用。

**搜索路径（按优先级）：**
1. `<项目>/agents/` — 项目级
2. `<项目>/.claude/agents/` — 兼容 Claude Code 项目结构

同名的自定义 Agent 会覆盖内置 Agent（Explore、Plan、Verification 等）。

**目录结构：**
```
agents/                       # 或 .claude/agents/
├── my-agent/
│   └── AGENT.md
└── code-reviewer/
    └── AGENT.md
```

**示例：**

```markdown
---
description: 快速搜索代码库的 agent
max_turns: 8
allowed_tools: bash, read_file, grep
disallowed_tools: write_file
---

你是一个代码搜索 agent。
根据用户的问题在代码库中查找相关内容，给出简洁的回答。
不要派生子 agent，直接执行。
```

| frontmatter 字段 | 必填 | 说明 |
|---|---|---|
| `description` | 是 | Agent 功能描述，用于 LLM 选择调用 |
| `name` | 否 | Agent 名称（省略则用目录名） |
| `max_turns` | 否 | 最大执行轮次（默认跟随全局配置） |
| `allowed_tools` | 否 | 工具白名单（逗号分隔），省略表示全部可用 |
| `disallowed_tools` | 否 | 工具黑名单（逗号分隔），在白名单基础上排除 |

---

### 权限配置 (permissions.json)

通过 JSON 文件控制工具的访问权限。

**配置文件位置：**
- 全局：`~/.my-agent/permissions.json`
- 项目：`<项目目录>/agent-permissions.json`（优先级更高）

**示例：**

```json
{
  "allow": [
    "bash(git *)",
    "bash(npm install*)",
    "read_file",
    "grep"
  ],
  "deny": [
    "bash(rm -rf*)",
    "bash(curl*)",
    "write_file(/etc/*)"
  ]
}
```

**规则格式：** `工具名` 或 `工具名(内容匹配模式)`

| 规则示例 | 含义 |
|---|---|
| `read_file` | 允许/拒绝所有 read_file 调用 |
| `bash(git *)` | 只匹配以 `git ` 开头的 bash 命令 |
| `write_file(/tmp/*)` | 只匹配写入 /tmp/ 目录的操作 |

**检查流程：** deny 规则优先扫描 → allow 规则扫描 → 无匹配则弹窗询问用户

**优先级：** session（运行时动态添加）> project（项目配置）> user（全局配置）> 默认（ask）

---

## 项目结构

```
src/
├── main.py              — CLI 入口
├── agent_loop.py        — Agent 主循环
├── api.py               — API 调用封装
├── display.py           — 终端展示层
├── tool_runner.py       — 统一工具执行入口
├── system_prompt.py     — 系统提示词构建
├── messages.py          — 消息构建辅助
├── permissions.py       — 权限控制
├── commands.py          — 斜杠命令处理
├── sandbox.py           — 沙箱执行环境
├── frontmatter.py       — YAML frontmatter 解析
├── watcher.py           — 文件监控 / 热重载
├── core/
│   ├── config.py        — 全局配置
│   ├── settings.py      — settings.json 管理
│   ├── types.py         — 核心数据结构
│   ├── events.py        — 事件系统
│   └── errors.py        — 错误处理
├── tools/               — 内置工具（bash, read, write, grep, glob, edit, agent, skill ...）
├── skills/              — Skill 发现 / 加载 / 缓存
├── agents/              — Subagent 定义（Explore, Plan, Verification ...）
├── memory/              — 记忆系统（扫描 / 召回 / 提取 / 提示构建）
├── compact/             — 上下文压缩（micro / auto / grouping）
├── providers/           — 多模型适配层（Anthropic / DeepSeek）
├── mcp_tool/            — MCP 工具集成
├── session/             — 会话持久化
└── utils/               — 通用工具函数
```

## License

MIT
