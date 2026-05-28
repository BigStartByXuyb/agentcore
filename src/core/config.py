"""Configuration loaded from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path

ANTHROPIC_AUTH_TOKEN: str = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
ANTHROPIC_BASE_URL: str | None = os.environ.get("ANTHROPIC_BASE_URL", None)

# Active provider — resolved by src/providers/get_provider() at call time.
# Override via env so users can swap backends without editing config.py.
PROVIDER: str = os.environ.get("AGENT_PROVIDER", "anthropic")

# DeepSeek
DEEPSEEK_API_KEY: str = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str | None = os.environ.get("DEEPSEEK_BASE_URL", None)
DEEPSEEK_REASONER_MODEL: str = os.environ.get("DEEPSEEK_REASONER_MODEL", "deepseek-reasoner")


# ---------------------------------------------------------------------------
# Model tiers — per-provider model configuration
# ---------------------------------------------------------------------------

@dataclass
class ProviderModels:
    """Model tiers for a provider.

    - provider: which provider these models belong to
    - main: primary model for the agent loop (user's choice)
    - compact: model for conversation summarization
    - side_query: cheap/fast model for memory recall, selection tasks
    - fallback: degradation target when main fails (thinking errors, etc.)
    """
    provider: str
    main: str
    compact: str
    side_query: str
    fallback: str


def get_models() -> ProviderModels:
    """Build ProviderModels from defaults + env overrides.

    Priority: env var > user's AGENT_MODEL > provider default.
    If AGENT_MODEL is set but AGENT_COMPACT_MODEL is not, compact follows main.
    Raises ValueError if PROVIDER is not registered in the provider registry.
    """
    from src.providers import get_provider

    adapter = get_provider(PROVIDER)
    defaults = adapter.get_default_models()

    user_main = os.environ.get("AGENT_MODEL")
    main = user_main or defaults.main
    compact = os.environ.get("AGENT_COMPACT_MODEL") or (main if user_main else defaults.compact)
    side_query = os.environ.get("AGENT_SIDE_QUERY_MODEL") or defaults.side_query
    fallback = os.environ.get("AGENT_FALLBACK_MODEL") or defaults.fallback

    return ProviderModels(
        provider=PROVIDER,
        main=main,
        compact=compact,
        side_query=side_query,
        fallback=fallback,
    )


MODELS: ProviderModels = get_models()
MODEL: str = MODELS.main  # backward-compatible alias
MAX_TOKENS: int = 16384
MAX_CONTEXT_WINDOW: int = 200_000  # model context window size (for auto compact threshold)
MAX_TURNS: int = 30
MAX_AGENT_DEPTH: int = 3  # Maximum nesting depth for sub-agents (fork skills / agent tool)

# Extended Thinking
THINKING_ENABLED: bool = True
THINKING_BUDGET_TOKENS: int = 10000  # budget_tokens for thinking (must be < MAX_TOKENS)

# Memory System
MEMORY_ENABLED: bool = True
MEMORY_MAX_FILES: int = 200          # max memory files to scan
MEMORY_MAX_RELEVANT: int = 5         # max memories to inject per turn

# Prompt Cache
PROMPT_CACHE_ENABLED: bool = True
PROMPT_CACHE_TTL_MINUTES: int = 5    # cache expiry threshold (minutes)

# Micro Compact (Layer 1 context compaction)
MICRO_COMPACT_ENABLED: bool = True
MICRO_COMPACT_KEEP_RECENT: int = 6   # keep last N rounds of tool_results intact

# Auto Compact (Layer 2 — LLM summarization)
AUTO_COMPACT_MAX_TOKENS: int = 4096  # max tokens for the summary response

# Token estimation
BYTES_PER_TOKEN: int = 2  # conservative: covers CJK (~1-2 token/char, 3 bytes) and English (~0.25 token/char, 1 byte)


def estimate_tokens(text: str) -> int:
    """Rough token estimate from UTF-8 byte length. Conservative (overestimates)."""
    return len(text.encode("utf-8")) // BYTES_PER_TOKEN


def tokens_to_chars(tokens: int) -> int:
    """Reverse of estimate_tokens: approximate character budget for a token limit."""
    return tokens * BYTES_PER_TOKEN


# Streaming fallback — retry as non-streaming when stream breaks mid-way
DISABLE_STREAMING_FALLBACK: bool = os.environ.get(
    "DISABLE_STREAMING_FALLBACK", ""
).lower() in ("1", "true")

# Sandbox (bash command isolation via bubblewrap)
SANDBOX_ENABLED: bool = True
SANDBOX_ALLOW_WRITE: list[str] = []           # additional writable paths (project dir + /tmp always allowed)
SANDBOX_DENY_WRITE: list[str] = []            # paths to force read-only inside sandbox
SANDBOX_DENY_READ: list[str] = []             # paths to hide inside sandbox
SANDBOX_EXCLUDED_COMMANDS: list[str] = []     # commands that skip sandboxing (e.g. "docker *")


# ---------------------------------------------------------------------------
# Unified directory paths — single source of truth for skills/agents/memory
# ---------------------------------------------------------------------------

def get_skill_dirs() -> list[str]:
    """Return ordered list of skill directories (project → global).

    Ensures each directory exists (auto-creates if missing).
    """
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
    """Return ordered list of agent directories (project-level).

    Ensures each directory exists (auto-creates if missing).
    """
    cwd = os.getcwd()
    dirs = [
        os.path.join(cwd, "agents"),
        os.path.join(cwd, ".claude", "agents"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    return dirs


def get_permission_config_paths() -> tuple[str, str]:
    """Return (user_config_path, project_config_path) for permissions."""
    home = str(Path.home())
    cwd = os.getcwd()
    user_config = os.path.join(home, ".my-agent", "permissions.json")
    project_config = os.path.join(cwd, "agent-permissions.json")
    return user_config, project_config