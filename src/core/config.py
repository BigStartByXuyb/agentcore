"""Configuration loaded from settings.json + environment variables.

Priority chain (lowest → highest):
  provider defaults → user settings.json → project settings.json → env var
"""

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# SESSION_ID — runtime state, not config. Mutable by /clear and /resume.
# ---------------------------------------------------------------------------
SESSION_ID: str = os.environ.get("AGENT_SESSION_ID", uuid.uuid4().hex[:12])


# ---------------------------------------------------------------------------
# Model tiers
# ---------------------------------------------------------------------------

@dataclass
class ProviderModels:
    provider: str
    main: str
    compact: str
    side_query: str
    fallback: str


# ---------------------------------------------------------------------------
# Config dataclass — single source of truth for ALL configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    # --- Provider & models ---
    provider: str = "anthropic"
    models: ProviderModels = field(default_factory=lambda: ProviderModels(
        provider="anthropic",
        main="claude-sonnet-4-6",
        compact="claude-sonnet-4-6",
        side_query="claude-haiku-3-5",
        fallback="claude-sonnet-4-6",
    ))
    model: str = "claude-sonnet-4-6"

    # --- API credentials (env-only) ---
    anthropic_auth_token: str = ""
    anthropic_base_url: str | None = None
    deepseek_api_key: str = ""
    deepseek_base_url: str | None = None
    deepseek_reasoner_model: str = "deepseek-reasoner"

    # --- Core limits (settings.json + env) ---
    max_tokens: int = 16384
    max_context_window: int = 200_000
    max_turns: int = 30
    max_agent_depth: int = 3

    # --- Extended Thinking ---
    thinking_enabled: bool = True
    thinking_budget_tokens: int = 10000

    # --- Memory ---
    memory_enabled: bool = True
    memory_max_files: int = 200
    memory_max_relevant: int = 5

    # --- Session ---
    session_persist_enabled: bool = True

    # --- Compaction ---
    micro_compact_enabled: bool = True
    micro_compact_keep_recent: int = 6
    auto_compact_max_tokens: int = 4096

    # --- Prompt cache (code constants, not user-configurable) ---
    prompt_cache_enabled: bool = True
    prompt_cache_ttl_minutes: int = 5

    # --- Token estimation (code constant) ---
    bytes_per_token: int = 2

    # --- Streaming ---
    disable_streaming_fallback: bool = False

    # --- Sandbox ---
    sandbox_enabled: bool = True
    sandbox_allow_write: list[str] = field(default_factory=list)
    sandbox_deny_write: list[str] = field(default_factory=list)
    sandbox_deny_read: list[str] = field(default_factory=list)
    sandbox_excluded_commands: list[str] = field(default_factory=list)

    # --- Utility methods (use instance fields, not module globals) ---

    def estimate_tokens(self, text: str) -> int:
        """Rough token estimate from UTF-8 byte length."""
        return len(text.encode("utf-8")) // self.bytes_per_token

    def tokens_to_chars(self, tokens: int) -> int:
        """Approximate character budget for a token limit."""
        return tokens * self.bytes_per_token


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _val(settings_val: Any, default: Any, env_key: str | None = None, cast: Any = None) -> Any:
    """Resolve value: env var > settings.json > code default."""
    if env_key:
        env = os.environ.get(env_key)
        if env is not None:
            return cast(env) if cast else env
    return settings_val if settings_val is not None else default


def _build_models(provider: str, settings: Any) -> ProviderModels:
    """Build ProviderModels from provider defaults → settings → env vars."""
    try:
        from src.providers import get_provider
        adapter = get_provider(provider)
        defaults = adapter.get_default_models()
    except Exception:
        @dataclass
        class _Defaults:
            main: str = "claude-sonnet-4-6"
            compact: str = "claude-sonnet-4-6"
            side_query: str = "claude-haiku-3-5"
            fallback: str = "claude-sonnet-4-6"
        defaults = _Defaults()

    s_main = settings.model
    env_main = os.environ.get("AGENT_MODEL")
    main = env_main or s_main or defaults.main

    s_compact = settings.compact_model
    compact = (
        os.environ.get("AGENT_COMPACT_MODEL")
        or s_compact
        or (main if (env_main or s_main) else defaults.compact)
    )
    side_query = (
        os.environ.get("AGENT_SIDE_QUERY_MODEL")
        or settings.side_query_model
        or defaults.side_query
    )
    fallback = (
        os.environ.get("AGENT_FALLBACK_MODEL")
        or settings.fallback_model
        or defaults.fallback
    )

    return ProviderModels(
        provider=provider, main=main, compact=compact,
        side_query=side_query, fallback=fallback,
    )


def _build_config() -> Config:
    """Read settings files + env vars, return a fully resolved Config."""
    from src.core.settings import load_settings
    s = load_settings()

    provider = _val(s.provider, "anthropic", "AGENT_PROVIDER")

    try:
        models = _build_models(provider, s)
    except Exception:
        models = ProviderModels(
            provider=provider,
            main="claude-sonnet-4-6", compact="claude-sonnet-4-6",
            side_query="claude-haiku-3-5", fallback="claude-sonnet-4-6",
        )

    persist_env = os.environ.get("AGENT_SESSION_PERSIST")
    if persist_env is not None:
        session_persist = persist_env != "0"
    elif s.session_persist_enabled is not None:
        session_persist = s.session_persist_enabled
    else:
        session_persist = True

    return Config(
        provider=provider,
        models=models,
        model=models.main,

        anthropic_auth_token=os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
        anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL", None),
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", None),
        deepseek_reasoner_model=os.environ.get("DEEPSEEK_REASONER_MODEL", "deepseek-reasoner"),

        max_tokens=_val(s.max_tokens, 16384),
        max_context_window=_val(s.max_context_window, 200_000),
        max_turns=_val(s.max_turns, 30),
        max_agent_depth=_val(s.max_agent_depth, 3),

        thinking_enabled=_val(s.thinking_enabled, True),
        thinking_budget_tokens=_val(s.thinking_budget_tokens, 10000),

        memory_enabled=_val(s.memory_enabled, True),
        memory_max_files=_val(s.memory_max_files, 200),
        memory_max_relevant=_val(s.memory_max_relevant, 5),

        session_persist_enabled=session_persist,

        micro_compact_enabled=_val(s.micro_compact_enabled, True),
        micro_compact_keep_recent=_val(s.micro_compact_keep_recent, 6),
        auto_compact_max_tokens=_val(s.auto_compact_max_tokens, 4096),

        prompt_cache_enabled=True,
        prompt_cache_ttl_minutes=5,
        bytes_per_token=2,

        disable_streaming_fallback=os.environ.get(
            "DISABLE_STREAMING_FALLBACK", ""
        ).lower() in ("1", "true"),

        sandbox_enabled=_val(s.sandbox_enabled, True),
        sandbox_allow_write=s.sandbox_allow_write if s.sandbox_allow_write is not None else [],
        sandbox_deny_write=s.sandbox_deny_write if s.sandbox_deny_write is not None else [],
        sandbox_deny_read=s.sandbox_deny_read if s.sandbox_deny_read is not None else [],
        sandbox_excluded_commands=s.sandbox_excluded_commands if s.sandbox_excluded_commands is not None else [],
    )


# ---------------------------------------------------------------------------
# Global singleton + accessors
# ---------------------------------------------------------------------------

_config: Config = _build_config()


def get() -> Config:
    """Return the current config. Always returns the latest after reload()."""
    return _config


def reload() -> None:
    """Re-read settings.json and rebuild the Config singleton."""
    global _config
    _config = _build_config()

    try:
        from src.sandbox import sandbox_manager
        sandbox_manager._config = None
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Directory paths
# ---------------------------------------------------------------------------

def get_skill_dirs() -> list[str]:
    cwd = os.getcwd()
    home = str(Path.home())
    dirs = [
        os.path.join(cwd, "skills"),
        os.path.join(cwd, ".claude", "skills"),
        os.path.join(home, ".claude", "skills"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    return dirs


def get_agent_dirs() -> list[str]:
    cwd = os.getcwd()
    dirs = [
        os.path.join(cwd, "agents"),
        os.path.join(cwd, ".claude", "agents"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    return dirs


def get_permission_config_paths() -> tuple[str, str]:
    home = str(Path.home())
    cwd = os.getcwd()
    user_config = os.path.join(home, ".my-agent", "permissions.json")
    project_config = os.path.join(cwd, "agent-permissions.json")
    return user_config, project_config
