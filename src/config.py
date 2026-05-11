"""Configuration loaded from environment variables."""

import os
from pathlib import Path

ANTHROPIC_AUTH_TOKEN: str = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
ANTHROPIC_BASE_URL: str | None = os.environ.get("ANTHROPIC_BASE_URL", None)

# Active provider — resolved by src/providers/get_provider() at call time.
# Override via env so users can swap backends without editing config.py.
PROVIDER: str = os.environ.get("AGENT_PROVIDER", "anthropic")

MODEL: str = "claude-sonnet-4-6"
MAX_TOKENS: int = 16384
MAX_CONTEXT_WINDOW: int = 200_000  # model context window size (for auto compact threshold)
MAX_TURNS: int = 30
MAX_AGENT_DEPTH: int = 3  # Maximum nesting depth for sub-agents (fork skills / agent tool)

# Extended Thinking
THINKING_ENABLED: bool = True
THINKING_BUDGET_TOKENS: int = 10000  # budget_tokens for thinking (must be < MAX_TOKENS)

# Memory System
MEMORY_ENABLED: bool = True
MEMORY_SIDE_QUERY_MODEL: str = "claude-haiku-4-5-20251001"  # cheap model for recall side query
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