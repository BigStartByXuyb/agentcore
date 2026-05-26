"""DeepSeek provider package — OpenAI-compatible backend for DeepSeek models.

Exports the DeepSeekAdapter and a module-level singleton helper,
mirroring the Anthropic provider's structure.
"""

from __future__ import annotations

from src.providers.deepseek.adapter import DeepSeekAdapter

_default_adapter: DeepSeekAdapter | None = None


def get_default_adapter() -> DeepSeekAdapter:
    """Return the process-wide DeepSeekAdapter, creating it on first call."""
    global _default_adapter
    if _default_adapter is None:
        _default_adapter = DeepSeekAdapter()
    return _default_adapter


def reset_default_adapter() -> None:
    """Drop the cached adapter (e.g. after credential change)."""
    global _default_adapter
    _default_adapter = None


__all__ = [
    "DeepSeekAdapter",
    "get_default_adapter",
    "reset_default_adapter",
]
