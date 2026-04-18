"""Anthropic provider package — native Claude API backend.

Exports the AnthropicAdapter and a module-level singleton helper so other
code (including the api.py dispatcher) can share one client instance per
process without re-creating it on every call.
"""

from __future__ import annotations

from src.providers.anthropic.adapter import AnthropicAdapter

_default_adapter: AnthropicAdapter | None = None


def get_default_adapter() -> AnthropicAdapter:
    """Return the process-wide AnthropicAdapter, creating it on first call."""
    global _default_adapter
    if _default_adapter is None:
        _default_adapter = AnthropicAdapter()
    return _default_adapter


def reset_default_adapter() -> None:
    """Drop the cached adapter (e.g. after credential change)."""
    global _default_adapter
    _default_adapter = None


__all__ = [
    "AnthropicAdapter",
    "get_default_adapter",
    "reset_default_adapter",
]
