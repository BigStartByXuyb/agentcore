"""Settings layer: schema, path resolution, file reading, merging, and defaults.

Priority chain (lowest → highest):
  code defaults → ~/.my-agent/settings.json (user) → .my-agent/settings.json (project)
"""
from __future__ import annotations

import json
import logging
import os
import warnings
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class SettingsSchema(BaseModel, extra="ignore"):
    """All fields optional. None means 'use code default'."""

    provider: Optional[str] = None
    model: Optional[str] = None
    compact_model: Optional[str] = None
    side_query_model: Optional[str] = None
    fallback_model: Optional[str] = None

    max_tokens: Optional[int] = None
    max_context_window: Optional[int] = None
    max_turns: Optional[int] = None
    max_agent_depth: Optional[int] = None

    thinking_enabled: Optional[bool] = None
    thinking_budget_tokens: Optional[int] = None

    memory_enabled: Optional[bool] = None
    memory_max_files: Optional[int] = None
    memory_max_relevant: Optional[int] = None

    session_persist_enabled: Optional[bool] = None

    micro_compact_enabled: Optional[bool] = None
    micro_compact_keep_recent: Optional[int] = None
    auto_compact_max_tokens: Optional[int] = None

    deepseek_reasoner_model: Optional[str] = None
    disable_streaming_fallback: Optional[bool] = None

    sandbox_enabled: Optional[bool] = None
    sandbox_allow_write: Optional[list[str]] = None
    sandbox_deny_write: Optional[list[str]] = None
    sandbox_deny_read: Optional[list[str]] = None
    sandbox_excluded_commands: Optional[list[str]] = None


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def get_settings_paths() -> tuple[str, str]:
    """Return (user_settings_path, project_settings_path).

    - user path: ~/.my-agent/settings.json
    - project path: <cwd>/.my-agent/settings.json
    """
    home = str(Path.home())
    cwd = os.getcwd()
    user_path = os.path.join(home, ".my-agent", "settings.json")
    project_path = os.path.join(cwd, ".my-agent", "settings.json")
    return user_path, project_path


def get_settings_file_paths() -> list[str]:
    """Return all settings file absolute paths (for watcher / hot-reload)."""
    user_path, project_path = get_settings_paths()
    return [user_path, project_path]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _read_settings_file(path: str) -> SettingsSchema:
    """Read and validate a single settings.json.

    Returns an empty (all-None) SettingsSchema on any error so callers
    always get a usable object:
    - File not found → silent empty schema
    - Malformed JSON → warning + empty schema
    - Validation error (wrong type, etc.) → warning + empty schema
    - Unknown keys are silently ignored (extra="ignore")
    """
    if not os.path.isfile(path):
        return SettingsSchema()

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as exc:
        warnings.warn(
            f"[settings] Malformed JSON in {path!r}: {exc}. Using defaults.",
            stacklevel=2,
        )
        return SettingsSchema()

    try:
        return SettingsSchema.model_validate(raw)
    except ValidationError as exc:
        warnings.warn(
            f"[settings] Validation error in {path!r}: {exc}. Using defaults.",
            stacklevel=2,
        )
        return SettingsSchema()


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def load_settings() -> SettingsSchema:
    """Load and merge user-level → project-level settings.

    Project non-None fields override user-level fields.
    Returns a merged SettingsSchema (may still have None fields —
    callers fall back to code-level defaults for those).
    """
    user_path, project_path = get_settings_paths()
    user_settings = _read_settings_file(user_path)
    proj_settings = _read_settings_file(project_path)

    # Start with user values, override with non-None project values
    merged = user_settings.model_dump()
    for field, value in proj_settings.model_dump().items():
        if value is not None:
            merged[field] = value

    return SettingsSchema.model_validate(merged)


# ---------------------------------------------------------------------------
# Defaults bootstrap
# ---------------------------------------------------------------------------

_SETTINGS_DESCRIPTIONS: dict[str, str] = {
    "provider":                 "模型提供商 (anthropic / deepseek)",
    "model":                    "主模型名称",
    "compact_model":            "上下文压缩用模型 (null=跟随主模型)",
    "side_query_model":         "记忆召回等轻量查询用模型 (null=用默认)",
    "fallback_model":           "主模型失败时的降级模型 (null=用默认)",
    "max_tokens":               "单次 LLM 回复的最大 token 数",
    "max_context_window":       "上下文窗口大小 (token)，超过触发压缩",
    "max_turns":                "单次对话最大轮次 (防止无限循环)",
    "max_agent_depth":          "子 Agent 最大嵌套深度",
    "thinking_enabled":         "Extended Thinking 开关",
    "thinking_budget_tokens":   "Thinking 预算 token 数 (必须 < max_tokens)",
    "memory_enabled":           "跨会话记忆系统开关",
    "memory_max_files":         "记忆目录最大文件扫描数",
    "memory_max_relevant":      "每轮召回的最大相关记忆数",
    "session_persist_enabled":  "会话持久化开关 (保存到本地 JSON)",
    "micro_compact_enabled":    "微压缩开关 (无损清理旧 tool_result)",
    "micro_compact_keep_recent":"微压缩保留最近 N 轮完整内容",
    "auto_compact_max_tokens":  "自动压缩摘要的最大 token 数",
    "deepseek_reasoner_model":  "DeepSeek 推理模型名称",
    "disable_streaming_fallback": "禁用流式降级 (true 时流式失败不自动回退)",
    "sandbox_enabled":          "沙箱隔离开关 (仅 Linux/WSL2 生效)",
    "sandbox_allow_write":      "沙箱额外允许写入的路径列表",
    "sandbox_deny_write":       "沙箱禁止写入的路径列表",
    "sandbox_deny_read":        "沙箱禁止读取的路径列表",
    "sandbox_excluded_commands": "不经过沙箱的命令列表",
}

_DEFAULT_SETTINGS: dict = {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "compact_model": None,
    "side_query_model": None,
    "fallback_model": None,
    "max_tokens": 16384,
    "max_context_window": 200000,
    "max_turns": 30,
    "max_agent_depth": 3,
    "thinking_enabled": True,
    "thinking_budget_tokens": 10000,
    "memory_enabled": True,
    "memory_max_files": 200,
    "memory_max_relevant": 5,
    "session_persist_enabled": True,
    "micro_compact_enabled": True,
    "micro_compact_keep_recent": 6,
    "auto_compact_max_tokens": 4096,
    "deepseek_reasoner_model": "deepseek-reasoner",
    "disable_streaming_fallback": False,
    "sandbox_enabled": True,
    "sandbox_allow_write": [],
    "sandbox_deny_write": [],
    "sandbox_deny_read": [],
    "sandbox_excluded_commands": [],
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_config() -> list[str]:
    """Validate the current effective config. Returns a list of error messages.

    Empty list = all good. Called at startup and after hot-reload.
    Checks: provider validity, API credentials, value ranges.
    """
    from src.core import config as cfg

    errors: list[str] = []
    c = cfg.get()

    # Provider must be registered
    from src.providers import list_providers
    known = list_providers()
    if c.provider not in known:
        errors.append(
            f"Unknown provider '{c.provider}'. "
            f"Available: {', '.join(known)}"
        )

    # API credential check per provider
    if c.provider == "anthropic" and not c.anthropic_auth_token:
        errors.append(
            "ANTHROPIC_AUTH_TOKEN not set. "
            "Set it via environment variable."
        )
    elif c.provider == "deepseek" and not c.deepseek_api_key:
        errors.append(
            "DEEPSEEK_API_KEY not set. "
            "Set it via environment variable."
        )

    # Value range checks
    if c.max_tokens < 1:
        errors.append(f"max_tokens must be >= 1, got {c.max_tokens}")
    if c.thinking_budget_tokens >= c.max_tokens:
        errors.append(
            f"thinking_budget_tokens ({c.thinking_budget_tokens}) "
            f"must be < max_tokens ({c.max_tokens})"
        )
    if c.max_turns < 1:
        errors.append(f"max_turns must be >= 1, got {c.max_turns}")
    if c.max_agent_depth < 1:
        errors.append(f"max_agent_depth must be >= 1, got {c.max_agent_depth}")

    return errors


# ---------------------------------------------------------------------------
# Defaults bootstrap
# ---------------------------------------------------------------------------

def _write_settings(path: str, values: dict | None = None) -> None:
    """Write settings to a JSON file with inline _comments block.

    If values is None, uses _DEFAULT_SETTINGS.
    The _comments key is ignored by pydantic (extra="ignore") on read-back.
    """
    source = values if values is not None else _DEFAULT_SETTINGS
    os.makedirs(os.path.dirname(path), exist_ok=True)
    output: dict = {"_comments": _SETTINGS_DESCRIPTIONS}
    for k, v in source.items():
        if v is not None:
            output[k] = v
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        f.write("\n")


def ensure_default_settings() -> None:
    """Create ~/.my-agent/settings.json with sensible defaults if it doesn't exist.

    Never overwrites an existing file.
    """
    user_path, _ = get_settings_paths()
    if os.path.isfile(user_path):
        return
    _write_settings(user_path)


def reset_user_settings() -> str:
    """Overwrite user-level settings with defaults. Returns the file path."""
    user_path, _ = get_settings_paths()
    _write_settings(user_path)
    return user_path


def create_project_settings() -> str:
    """Create project-level settings inheriting current effective config.

    Reads the merged config (code defaults + user settings) and writes it
    to the project-level file, so the user starts with the full effective
    config and only needs to change what differs for this project.

    If the file already exists, does nothing and returns the path.
    """
    _, project_path = get_settings_paths()
    if os.path.isfile(project_path):
        return project_path

    merged = load_settings()
    effective: dict = {}
    for key, default_val in _DEFAULT_SETTINGS.items():
        merged_val = getattr(merged, key, None)
        effective[key] = merged_val if merged_val is not None else default_val

    _write_settings(project_path, effective)
    return project_path
