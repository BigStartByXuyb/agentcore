"""Configuration loaded from environment variables."""

import os

ANTHROPIC_AUTH_TOKEN: str = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
ANTHROPIC_BASE_URL: str | None = os.environ.get("ANTHROPIC_BASE_URL", None)

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
