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