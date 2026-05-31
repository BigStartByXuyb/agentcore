# Task Progress

## Steps 1-3: DONE (2026-04-13)

项目骨架 + 基础工具 + Agent Loop 端到端跑通。

### 已完成
- [x] 目录结构: src/, src/tools/, tests/, docs/
- [x] config.py: 环境变量加载
- [x] types.py: ToolResult, ToolUseContext, AgentState, ToolDef
- [x] tools/bash.py: shell 命令执行
- [x] tools/read_file.py: 文件读取 + 行号
- [x] tools/grep.py: ripgrep/grep 搜索
- [x] tools/__init__.py: ALL_TOOLS 注册表
- [x] system_prompt.py: 系统提示构建
- [x] messages.py: 消息辅助函数
- [x] tool_runner.py: 统一工具执行入口
- [x] agent_loop.py: LLM ↔ 工具循环
- [x] main.py: CLI 入口
- [x] 端到端测试: bash/read_file/grep 均通过

## Step 4: DONE (2026-04-13)

Skill 系统 (inline 模式) 完整实现。

### 已完成
- [x] src/skills/__init__.py: Skill 发现与加载模块
  - parse_frontmatter(): YAML frontmatter 解析
  - SkillInfo 数据结构 (name, description, is_fork, allowed_tools, body, base_dir, when_to_use)
  - discover_skills(): 扫描 skills/ 目录
  - build_skill_content(): ${SKILL_DIR} / ${CLAUDE_SKILL_DIR} 变量替换 + base_dir 头部
  - format_skill_listing(): 生成 system-reminder 文本
  - get_skills() / get_skill(): 缓存式访问
- [x] src/tools/skill.py: Skill 工具定义 (ToolDef)
  - schema: LLM 可见的工具描述
  - executor: inline 模式返回 new_messages + context_modifier
  - map_result: "Launching skill: <name>"
  - 支持 /slash 前缀、$ARGUMENTS 替换
- [x] agent_loop.py: 处理 context_modifier
  - 收集 pending_context_modifier
  - 应用后重建 tool schemas (支持工具白名单)
- [x] messages.py: build_tool_schemas() 支持 allowed_tools 过滤
  - Skill 工具始终包含在白名单中
- [x] system_prompt.py: 注入 skill 列表到系统提示
- [x] tools/__init__.py: 注册 Skill 工具
- [x] skills/hello-world/SKILL.md: 测试用 skill
- [x] skills/read-only/SKILL.md: 测试工具白名单
- [x] tests/test_skills.py: 11 个测试全部通过
- [x] pyproject.toml: 添加 pyyaml 依赖

## Skill listing 注入位置修正 (2026-04-13)

将 skill listing 从 system prompt 挪到 user message 注入，对齐 Claude Code 源码架构。

### 改动
- [x] system_prompt.py: 去掉 skill listing 拼接，保持 system prompt 稳定可缓存
- [x] skills/__init__.py: 新增 build_skill_reminder() + reset_sent_skills()
  - 生成 `<system-reminder>` 包裹的 user message
  - _sent_skill_names 追踪已发送的 skill，避免重复发送
  - force=True 支持强制重发（用于 /clear 场景）
- [x] agent_loop.py: 用户消息后注入 skill reminder（user message）
  - 利用 normalized_for_api() 自动合并相邻 user message
- [x] tests/test_skills.py: 新增 4 个测试（共 15 个全部通过）

### 下一步: Step 5 — Skill fork 模式

## Step 5: DONE (2026-04-14)

Skill fork 模式完整实现。

### 已完成
- [x] src/config.py: 新增 MAX_AGENT_DEPTH = 3（递归深度限制）
- [x] src/agent_loop.py: 重构 — 抽取 run_agent_loop() 低层函数
  - run_agent_loop(): 可复用的 LLM ↔ tool 循环，接受原始 messages
  - agent_loop(): 变为薄封装（管理 MessageHistory、注入 skill reminder、委托 run_agent_loop）
  - _wrap_messages(): 用已有 list 创建 MessageHistory（共享引用，不拷贝）
  - _serialize_content(): 从辅助逻辑中抽出，复用
  - 支持 label 参数区分主循环和子 agent 的终端输出
- [x] src/tools/skill.py: 实现 _execute_fork()
  - 深度检查: context.depth >= MAX_AGENT_DEPTH → 返回错误
  - 构建子 agent 上下文: 独立的 messages、system_prompt、tools、state
  - 子 system_prompt: 明确禁止子 agent 派生新的子 agent
  - 工具白名单: 通过 build_tool_schemas(skill.allowed_tools) 限制
  - 异常处理: try/except 包裹 run_agent_loop，优雅返回错误
  - 返回 ToolResult(data={status:"forked", result:...})，无 new_messages/context_modifier
  - 原 _execute() 拆分为 _execute_inline() + _execute_fork() 两个清晰分支
- [x] tests/fixtures/skills/fork-test/SKILL.md: fork 模式测试 fixture
- [x] tests/fixtures/skills/fork-unrestricted/SKILL.md: 无工具限制的 fork fixture
- [x] tests/test_skills.py: 新增 10 个 fork 相关测试（共 24 个全部通过）
  - test_discover_fork_skill / test_discover_fork_unrestricted_skill
  - test_skill_executor_fork_basic (mock run_agent_loop，验证完整调用链)
  - test_skill_executor_fork_with_args ($ARGUMENTS 替换)
  - test_skill_executor_fork_allowed_tools (工具白名单传递)
  - test_skill_executor_fork_depth_limit (深度限制拦截)
  - test_skill_executor_fork_exception_handling (异常优雅处理)
  - test_map_result_forked / test_map_result_forked_empty_result / test_map_result_fork_error

### 架构说明
- run_agent_loop() 是 agent_loop() 和 fork skill 的共用底层循环
- fork skill 的 executor 在内部调用 run_agent_loop()，同步阻塞等待结果
- 深度通过 ToolUseContext.depth 传递，每层 +1
- 子 agent 的 system_prompt 明确禁止派生（prompt 级 + depth 限制双重保障）

### 下一步: Step 6 — Subagent 工具

## Step 6: DONE (2026-04-14)

Subagent 工具完整实现。

### 已完成
- [x] src/agents/__init__.py: AgentDefinition 数据结构 + 注册表
  - AgentDefinition dataclass (name, description, system_prompt, max_turns, allowed_tools, disallowed_tools)
  - register_agent() / get_agent() / list_agents() — 大小写不敏感查找
  - 自动注册内置 agent（import 时执行）
- [x] src/agents/explore.py: Explore agent 定义
  - 只读工具: bash, read_file, grep
  - system_prompt: 代码搜索专家，禁止派生子 agent
  - max_turns: 12
- [x] src/tools/agent.py: Agent 工具定义 (ToolDef)
  - AGENT_SCHEMA: LLM 可见的工具描述 (description, prompt, agent_type)
  - _DEFAULT_AGENT: 通用 agent（无工具限制，max_turns=20）
  - _resolve_tools(): 构建子 agent 工具列表，始终排除 "agent" 自身
  - _execute(): 解析 agent_type → 深度检查 → 构建上下文 → run_agent_loop()
  - _map_result(): 成功返回子 agent 输出，失败返回错误信息
- [x] src/tools/__init__.py: 注册 agent_tool 到 ALL_TOOLS
- [x] tests/test_agent_tool.py: 15 个测试全部通过
  - 注册表: registered/case_insensitive/unknown/list
  - Schema: 结构验证
  - _resolve_tools: agent 排除 / 白名单过滤
  - Executor: default_agent / explore / unknown_type / no_prompt / depth_limit / exception
  - map_result: success / error
- [x] 全量回归: 39 个测试全部通过（24 skill + 15 agent）

### 架构说明
- Agent 工具复用 fork skill 的子 agent 机制（run_agent_loop + depth 限制）
- AgentDefinition 独立于 tools/ 目录，方便后续添加更多 agent 类型
- "agent" 工具始终从子 agent 工具列表中排除（防无限递归）
- 深度限制与 fork skill 共享 MAX_AGENT_DEPTH（prompt + 工具过滤双重保障）

## Step 6 增强: Agent 外部加载 (2026-04-14)

对齐 Skill 的文件发现机制，Agent 支持从 AGENT.md 文件加载。

### 已完成
- [x] src/agents/__init__.py: 重构为文件发现模式
  - 复用 Skill 的 parse_frontmatter() 解析 AGENT.md
  - _load_agent_from_dir(): 从目录加载单个 agent
  - _parse_allowed_tools() / _parse_disallowed_tools(): frontmatter 字段解析
  - discover_agents(): 扫描 agents/ 和 .claude/agents/ 目录
  - get_agents() / get_agent(): 带缓存的访问（同 get_skills 模式）
  - 内置 agent (Explore) 始终存在，外部同名可覆盖
  - 删除 register_agent() 公开 API
- [x] src/tools/agent.py: AGENT_SCHEMA description 动态列出可用 agent 类型
  - _build_agent_description(): import 时生成，包含所有已发现的 agent
- [x] tests/fixtures/agents/test-agent/AGENT.md: 测试 fixture (max_turns=8, allowed_tools=bash,read_file)
- [x] tests/fixtures/agents/custom-explore/AGENT.md: 覆盖测试 fixture (max_turns=6, allowed_tools=bash)
- [x] tests/test_agent_tool.py: 扩展到 21 个测试（新增 6 个文件发现测试）
  - test_load_agent_from_dir / test_load_agent_missing_dir
  - test_discover_agents_from_fixture / test_discover_agents_override_builtin
  - test_discover_agents_builtin_always_present / test_schema_lists_agents_dynamically
- [x] 全量回归: 45 个测试全部通过（24 skill + 21 agent）

## 错误处理规范化 (2026-04-14)

对齐 Claude Code 源码的错误处理机制。

### 已完成
- [x] src/errors.py: 新建 — AgentErrorCode 枚举 + classify_api_error() + create_assistant_error_message()
  - 11 个错误码（API 层 8 个 + 工具层 3 个）
  - classify_api_error(): APIConnectionError → timeout/connection, APIError → 按 status_code 分类
  - create_assistant_error_message(): 生成 synthetic assistant 消息，保持消息交替
- [x] src/messages.py: build_tool_result_content() 新增 is_error 参数
  - is_error=True 时用 `<tool_use_error>` 包裹内容 + 设置 is_error 字段
- [x] src/tool_runner.py: run_tool_use() 返回 3-tuple (ToolResult, str, bool)
  - executor 和 map_result 分开 try/catch，错误定位更精确
  - 未知工具 / executor 异常 / map_result 异常 → is_error=True
- [x] src/agent_loop.py: 三处改动
  - query_model() 外层 try/except → 注入 synthetic assistant 消息 → 返回错误文本
  - tool_result 传递 is_error 标记
  - _recover_orphan_tool_results(): 补全缺失的 tool_result（孤儿 tool_use 恢复）
- [x] tests/test_errors.py: 28 个测试全部通过
  - classify_api_error: 12 个场景（connection/timeout/429/529/401/403/400/500/502/418/generic）
  - create_assistant_error_message: 4 个（结构/详情/auth/rate_limit）
  - build_tool_result_content: 3 个（成功/显式false/error）
  - run_tool_use: 4 个（未知工具/executor异常/map异常/成功）
  - _recover_orphan_tool_results: 3 个（无孤儿/有孤儿/无tool_use）
  - agent_loop API 恢复: 2 个（注入synthetic消息/不重试）
- [x] 全量回归: 78 个测试全部通过

### 数据流变化
```
之前: executor → data → map_result → 纯文本 → tool_result(无标记)
之后: executor → data → map_result → 文本 → tool_result(is_error=False)
      executor 💥 → 错误文本 → tool_result(is_error=True, <tool_use_error>包裹)

之前: query_model() 💥 → 异常抛到 main → history 断裂
之后: query_model() 💥 → synthetic assistant message → history 完整 → 返回错误文本
```

### 开发顺序全部完成
Steps 1-6（含增强）+ 错误处理规范化均已实现并测试通过。

## Extended Thinking 支持: DONE (2026-04-14)

### 已完成
- [x] src/config.py: 新增 THINKING_ENABLED / THINKING_BUDGET_TOKENS 配置
- [x] src/types.py: AgentState 新增 total_thinking_tokens 字段
- [x] src/api.py: query_model / query_model_stream 新增 thinking 可选参数
  - 条件传入 API 调用（dict 解包方式，不传时不影响原有行为）
- [x] src/messages.py: 新增 thinking 历史清洗函数
  - _is_thinking_block(): 判断 thinking/redacted_thinking 块
  - strip_thinking_blocks(): 从 assistant 消息中移除 thinking 块（保留其他内容）
  - filter_orphaned_thinking_messages(): 移除只含 thinking 块或空内容的 assistant 消息
- [x] src/agent_loop.py: 5 处改动
  - _serialize_content(): 处理 thinking + redacted_thinking 块（保留 signature）
  - run_agent_loop(): 接受 thinking 参数并传给 API
  - _print_thinking(): 终端展示 thinking 内容（截断 200 字符）
  - token 统计: 从 response.usage 提取 thinking tokens
  - 400 错误恢复: _is_thinking_400() 检测 + _clean_thinking_history() 清洗 + 重试一次
  - _build_thinking_param(): 构建 thinking 参数（budget_tokens 不超过 max_tokens-1）
  - agent_loop() 顶层入口: 调用 _build_thinking_param() 传给 run_agent_loop()
- [x] tests/test_thinking.py: 37 个测试全部通过
  - config 默认值 (3)
  - _is_thinking_block (4)
  - _serialize_content thinking 块 (4)
  - _build_thinking_param 逻辑 (4)
  - _is_thinking_400 检测 (5)
  - strip_thinking_blocks (5)
  - filter_orphaned_thinking_messages (4)
  - _clean_thinking_history 原地修改 (1)
  - _print_thinking 展示 (3)
  - AgentState thinking tokens (2)
  - run_agent_loop 400 恢复 (2)
- [x] 全量回归: 112 passed, 3 pre-existing failures (非 thinking 相关)

### 架构说明
- thinking 参数通过 dict 解包传入 SDK，不传时等同于禁用
- thinking 块的 signature 必须原样保留发回 API，否则 400
- 400 错误恢复: 检测 "invalid signature" / "thinking blocks cannot be modified" → strip + filter → 重试一次
- 子 agent (fork skill / agent tool) 不传 thinking 参数（静默执行，无需 thinking）

## Agent Event System — Generator yield 模式: DONE (2026-04-15)

将所有 print() 从核心逻辑中移除，改为 generator yield 事件对象，外层消费者决定展示方式。

### 已完成
- [x] src/events.py: 新建 — AgentEvent dataclass 层级
  - AgentEvent 基类 (label 字段)
  - TextDelta / TextBlock / ThinkingBlock / ToolStart
  - ErrorEvent / Recovery / TokenUsage / RetryNotice
  - SubAgentStart / SubAgentEnd
- [x] src/display.py: 新建 — 默认终端展示 handler
  - default_handler(): 按事件类型 print 到终端（所有原 print 逻辑集中于此）
  - consume_events(): 迭代 generator、分发事件、返回最终文本
  - _summarize_input(): 从 agent_loop.py 迁移
- [x] src/agent_loop.py: 核心改动
  - run_agent_loop() 返回类型: str → Generator[AgentEvent, None, str]
  - 所有 print() 替换为 yield 对应事件
  - streaming text delta 收集到 list 后统一 yield（回调内不能 yield）
  - agent_loop() 用 consume_events(gen, default_handler) 消费
  - 删除 _print_tokens / _print_thinking / _summarize_input（已迁移到 display.py）
- [x] src/api.py: query_model / query_model_stream 新增 on_retry 回调
  - print() 替换为 on_retry(delay, attempt, max_attempts) 回调
  - agent_loop 传入闭包收集 RetryNotice 事件
- [x] src/tools/skill.py: fork 模式适配
  - run_agent_loop() → consume_events(gen, handler=None) 静默消费
  - 删除 print() 调用
- [x] src/tools/agent.py: 同上
- [x] 测试更新:
  - test_thinking.py: _print_thinking 测试改为 ThinkingBlock + default_handler 测试
  - test_errors.py: run_agent_loop 调用包裹 consume_events()
  - test_skills.py / test_agent_tool.py: mock 改为返回 generator（_mock_run_agent_loop helper）
- [x] 全量回归: 112 passed, 3 pre-existing failures

### 架构说明
- run_agent_loop() 是 generator，yield AgentEvent，return str（通过 StopIteration.value）
- consume_events(gen, handler) 是通用消费器：迭代事件 → 调 handler → 返回 return value
- handler=None 时静默消费（子 agent 场景）
- handler=default_handler 时打印到终端（主 agent 场景）
- api.py 用 on_retry 回调而非 generator（改动最小化）
- print() 只存在于 display.py（展示层）和 main.py（REPL UI）

## 真流式 + 事件冒泡 + ToolEnd 重构: DONE (2026-04-15)

解决 4 个问题: 假流式、子 agent 黑盒、缺 ToolEnd、丑陋的 "first" 标记。

### 已完成
- [x] src/events.py: 新增 ToolEnd 事件
- [x] src/display.py: 新增 ToolEnd handler
- [x] src/api.py: 新增 create_stream_with_retry() — 返回 MessageStreamManager（不进入 with）
- [x] src/agent_loop.py: 真流式重构
  - stream 分支改为 yield from _stream_call()（直接迭代 text_stream，逐 token yield TextDelta）
  - 删除 _streamed_text_deltas list、_on_text 回调、("first" if ...) hack
  - 工具执行改为 yield from run_tool_use() + yield ToolEnd
  - thinking-400 recovery stream 分支同步更新
- [x] src/tool_runner.py: 变为 generator
  - hasattr(__next__) 区分普通 executor 和 generator executor
  - generator executor 通过 yield from 冒泡子 agent 事件
  - 普通工具（bash/read_file/grep）零改动
- [x] src/tools/agent.py: _execute 变 generator
  - 替换 consume_events(gen, None) 为 yield from gen
- [x] src/tools/skill.py: _execute 变 generator（fork 路径）
  - fork 分支: return (yield from _execute_fork(...))
  - inline 分支: return _execute_inline(...)（不变）
- [x] tests/test_agent_tool.py: 新增 _drain helper，包裹所有 _execute() 调用
- [x] tests/test_skills.py: 同上，9 处 _execute() 调用包裹
- [x] tests/test_errors.py: 4 处 run_tool_use() 调用包裹 _drain()
- [x] 全量回归: 112 passed, 3 pre-existing failures（非本次改动相关）
- [x] CLAUDE.md 架构文档更新

### 架构说明
- 真流式: _stream_call() 在 generator 内直接迭代 SDK text_stream，逐 token yield TextDelta
- 事件冒泡: 子 agent 事件通过 yield from 链透传到最外层 consume_events()
- ToolEnd: 每个工具执行完成后 yield ToolEnd（含 is_error + result_summary）
- generator executor 检测: hasattr(__next__) 鸭子类型，普通工具零改动
- with stream_cm: 管理 HTTP 长连接生命周期（进入建立连接，退出关闭 socket）

## Memory System: DONE (2026-04-15)

三层记忆架构，对齐 Claude Code 源码 src/memdir/ 体系。

### 已完成
- [x] src/types.py: 新增 MemoryType (Literal) + MemoryHeader dataclass
- [x] src/config.py: 新增 MEMORY_ENABLED / MEMORY_SIDE_QUERY_MODEL / MEMORY_MAX_FILES / MEMORY_MAX_RELEVANT
- [x] src/memory/__init__.py: 包初始化
- [x] src/memory/paths.py: get_memory_dir / get_memory_entrypoint / ensure_memory_dir / is_memory_path
- [x] src/memory/scan.py: scan_memory_files (frontmatter 解析) + format_memory_manifest
- [x] src/memory/prompt.py: build_memory_prompt — 行为指令 + MEMORY.md 内容
- [x] src/memory/recall.py: find_relevant_memories — side query (Haiku) 选择相关记忆
- [x] src/memory/extract.py: run_memory_extraction — 后台 forked agent 提取记忆
- [x] src/api.py: 新增 side_query() — 轻量 LLM 调用（无 tools/thinking/streaming）
- [x] src/system_prompt.py: 追加记忆行为指令到 system prompt
- [x] src/agent_loop.py: 注入选中记忆 + 触发后台提取
- [x] CLAUDE.md: 更新文件结构（新增 memory/ 模块）

### 架构说明
- Layer 1 (recall): side_query() 用 Haiku 扫描记忆文件 frontmatter，选最多 5 个相关文件
- Layer 2 (injection): 行为指令在 system prompt（稳定可缓存），选中记忆内容在 user message（<system-reminder>）
- Layer 3 (extraction): 每轮 end_turn 后 forked agent 提取记忆，与主 agent 写入互斥
- Subagent 不继承主 agent 记忆
- 记忆目录: ~/.my-agent/memory/，MEMORY.md 为索引文件

## 待做: WebFetch/WebSearch 工具 (计划中，不急)

为 agent 添加专用的网页抓取和搜索工具，让模型能查阅网页内容（不同于 bash curl 的原始输出）。

## 待做: 会话持久化 (计划中)

当前所有会话状态都是纯内存的，程序退出即丢失。需要实现持久化：

### 需要持久化的状态
1. **MessageHistory** — 对话历史（Message.to_serializable/from_serializable 已就绪）
2. **AgentState** — token 统计、agent_id、plan 状态
3. **Task store** — 任务列表
4. **FileStateCache** — 文件状态缓存（可选，重建成本低）

### 设计方向（待确认）
- 会话目录: `~/.my-agent/sessions/<session_id>/`
- 每个会话一个 JSON 文件（conversation.json）或按轮次追加（JSONL）
- 启动时可选恢复上次会话（`--resume` / `--last`）
- /clear 时归档当前会话而非直接丢弃
- 参考 Claude Code 的 session 持久化机制
