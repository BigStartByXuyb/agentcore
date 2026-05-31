# Settings Layer Design Spec

## 目标

将 `config.py` 中的硬编码配置外部化为 `settings.json`，支持用户级/项目级两层配置文件，通过 watchdog 热重载，使用 pydantic 进行字段校验。

## 文件位置

| 层级 | 路径 | 用途 |
|------|------|------|
| 用户级 | `~/.my-agent/settings.json` | 全局偏好（模型、thinking 等） |
| 项目级 | `{cwd}/.my-agent/settings.json` | 项目特殊配置 |

## 优先级（低 → 高）

```
代码默认值 → 用户级 settings.json → 项目级 settings.json → 环境变量
```

- 环境变量只覆盖启动项（API key、base_url、provider）和临时覆盖（AGENT_MODEL 等）
- 模型选择等行为配置主要走 settings.json

## settings.json 字段定义

所有字段可选，缺失的用代码默认值。未知字段忽略（forward-compatible）。

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "compact_model": null,
  "side_query_model": null,
  "fallback_model": null,
  "max_tokens": 16384,
  "max_context_window": 200000,
  "max_turns": 30,
  "max_agent_depth": 3,
  "thinking_enabled": true,
  "thinking_budget_tokens": 10000,
  "memory_enabled": true,
  "memory_max_files": 200,
  "memory_max_relevant": 5,
  "session_persist_enabled": true,
  "micro_compact_enabled": true,
  "micro_compact_keep_recent": 6,
  "auto_compact_max_tokens": 4096,
  "sandbox_enabled": true,
  "sandbox_allow_write": [],
  "sandbox_deny_write": [],
  "sandbox_deny_read": [],
  "sandbox_excluded_commands": []
}
```

**不进 settings.json 的配置：**

| 配置 | 原因 | 保留方式 |
|------|------|---------|
| API key / base_url | 敏感信息 | 环境变量 |
| PROMPT_CACHE_TTL_MINUTES | 服务端固定值 | 代码常量 |
| BYTES_PER_TOKEN | 内部估算常量 | 代码常量 |
| SESSION_ID | 运行时生成 | 代码生成 |

## 架构

### 新增文件

**`src/core/settings.py`** — 读取、合并、校验

职责：
1. 解析 settings.json 文件路径
2. 读取 JSON 文件
3. 两层合并（用户级 → 项目级覆盖）
4. Pydantic 模型校验
5. 返回校验后的 Settings 对象

### 改造文件

**`src/core/config.py`** — 初始化改造 + reload()

改造内容：
1. 模块加载时调 `settings.load_settings()` 获取配置
2. 用 settings 值覆盖默认值（环境变量仍有最高优先级）
3. 新增 `reload()` 函数：重新读取 settings → 刷新模块级变量

**`src/watcher.py`** — 新增 settings 文件监听

改造内容：
1. 新增 settings 文件的 watcher（复用 `_McpConfigHandler` 模式）
2. 防抖 0.5 秒
3. 回调调用 `config.reload()`

## 核心 API

### settings.py

```python
from pydantic import BaseModel

class SettingsSchema(BaseModel):
    """Settings 字段定义与校验。所有字段可选，缺失用默认值。"""
    provider: str | None = None
    model: str | None = None
    compact_model: str | None = None
    side_query_model: str | None = None
    fallback_model: str | None = None
    max_tokens: int | None = None
    max_context_window: int | None = None
    max_turns: int | None = None
    max_agent_depth: int | None = None
    thinking_enabled: bool | None = None
    thinking_budget_tokens: int | None = None
    memory_enabled: bool | None = None
    memory_max_files: int | None = None
    memory_max_relevant: int | None = None
    session_persist_enabled: bool | None = None
    micro_compact_enabled: bool | None = None
    micro_compact_keep_recent: int | None = None
    auto_compact_max_tokens: int | None = None
    sandbox_enabled: bool | None = None
    sandbox_allow_write: list[str] | None = None
    sandbox_deny_write: list[str] | None = None
    sandbox_deny_read: list[str] | None = None
    sandbox_excluded_commands: list[str] | None = None

def get_settings_paths() -> tuple[str, str]:
    """返回 (用户级路径, 项目级路径)"""

def load_settings() -> SettingsSchema:
    """读取、合并、校验 settings.json。
    合并策略：浅合并，项目级非 None 字段覆盖用户级。
    校验失败的字段 warning 并忽略（用默认值）。
    """

def get_settings_file_paths() -> list[str]:
    """返回所有 settings 文件的绝对路径（给 watcher 用）"""

def ensure_default_settings() -> None:
    """首次运行时创建 ~/.my-agent/settings.json（带默认值）。
    文件已存在则不覆盖。"""
```

### config.py 改造

```python
def reload() -> None:
    """从 settings.json 重新加载所有可配置项。
    1. 调 load_settings()
    2. 刷新模块级变量（PROVIDER, MODEL, MAX_TOKENS 等）
    3. 环境变量仍保持最高优先级
    4. 刷新 MODELS（重新调 get_models()）
    5. 刷新 sandbox_manager 配置
    """
```

## 合并策略

浅合并，非 None 值覆盖：

```python
defaults = SettingsSchema()           # 全部 None
user_settings = _read_file(user_path) # 用户级
proj_settings = _read_file(proj_path) # 项目级

# 用户级非 None 字段覆盖 defaults
# 项目级非 None 字段覆盖 用户级
merged = {}
for field in SettingsSchema.model_fields:
    proj_val = getattr(proj_settings, field)
    user_val = getattr(user_settings, field)
    merged[field] = proj_val if proj_val is not None else user_val

return SettingsSchema(**merged)
```

## 热重载

### 数据流

```
settings.json 文件变更
  → watchdog 检测（OS 级 fs event）
  → _DebouncedNotifier（0.5s 防抖）
  → _reload_settings() 回调（asyncio event loop 中执行）
  → config.reload()
    → settings.load_settings()      # 重读磁盘 + pydantic 校验
    → 刷新 config.MODEL 等模块级变量
    → 刷新 sandbox_manager 配置
  → 消费端下次读 config.XXX 时自动拿到新值
```

### 生效时机

| 配置类别 | 生效时机 | 原因 |
|---------|---------|------|
| model / thinking / max_tokens | 下一轮 LLM 调用 | agent loop 循环开头读 config |
| memory_enabled / compact 相关 | 下一轮对话 | 同上 |
| sandbox 相关 | 下一次 bash 执行 | sandbox_manager 每次调用读 config |
| permissions | 已有独立热重载 | 不经过 settings 层 |

### watcher 集成

```python
SETTINGS_DEBOUNCE = 0.5

def _reload_settings() -> None:
    from src.core import config
    config.reload()
    log.info("[watcher] Settings reloaded.")

# 在 start_watchers() 中新增：
settings_notifier = _DebouncedNotifier(loop, _reload_settings, SETTINGS_DEBOUNCE)
settings_paths = get_settings_file_paths()
settings_handler = _McpConfigHandler(
    {os.path.basename(p) for p in settings_paths},
    settings_notifier,
)
# 监听 settings 文件所在目录
```

## 初始化创建

首次运行时，`ensure_default_settings()` 在 `~/.my-agent/settings.json` 创建默认配置：

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "thinking_enabled": true,
  "thinking_budget_tokens": 10000,
  "memory_enabled": true
}
```

只包含最常调整的字段，其余用代码默认值。文件已存在则不覆盖。

## Model 优先级细节

`get_models()` 改造后的优先级（低 → 高）：

```
provider 默认模型 → settings.json 的 model → 环境变量 AGENT_MODEL
```

具体逻辑：
1. `settings.model` 非 None → 覆盖 provider 默认值
2. `AGENT_MODEL` 环境变量存在 → 覆盖 settings.model
3. `compact_model` / `side_query_model` / `fallback_model` 同理
4. `reload()` 时重新执行此逻辑，刷新 `config.MODELS` 和 `config.MODEL`

## 不做的事情

| 不做 | 原因 |
|------|------|
| Settings 写入 API | 未来可加，热重载保证文件改动自动生效 |
| 信号/订阅系统 | 没有 UI 需要响应式更新，直接改全局变量足够 |
| Settings 迁移 | 第一版，无历史数据 |
| 深层合并 | settings 结构扁平，无嵌套对象 |

## 依赖

- `pydantic` — 需要新增到 requirements.txt（如未安装）
- `watchdog` — 已有依赖

## 测试策略

- 单元测试 `settings.py`：读取、合并、校验、缺失文件处理
- 单元测试 `config.reload()`：settings 覆盖 + 环境变量优先级
- 集成测试：修改 settings 文件 → 验证 config 变量已更新
