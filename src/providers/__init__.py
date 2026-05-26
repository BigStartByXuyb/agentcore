"""Provider registry — resolves a provider name to its adapter instance.

Every provider backend (Anthropic, OpenAI-compat, Google, ...) lives in
its own sub-package and exposes a `get_default_adapter()` function. The
registry below maps config.PROVIDER (a string) to that resolver.

Adding a new provider:
    1. Create src/providers/<name>/ with an adapter implementing
       ProviderAdapter (see src/providers/base.py).
    2. Expose a get_default_adapter() in the package __init__.
    3. Register it in the _PROVIDERS map below.

The dispatcher in src/api.py uses get_provider(config.PROVIDER) to pick
which adapter handles the active request — zero provider-specific
branches anywhere else in the codebase.
"""

from __future__ import annotations

from typing import Callable

from src.providers.base import ProviderAdapter


# name → zero-arg factory returning the adapter singleton
_PROVIDERS: dict[str, Callable[[], ProviderAdapter]] = {}


def _register_anthropic() -> None:
    from src.providers.anthropic import get_default_adapter as _anthropic_factory
    _PROVIDERS["anthropic"] = _anthropic_factory


def _register_deepseek() -> None:
    from src.providers.deepseek import get_default_adapter as _deepseek_factory
    _PROVIDERS["deepseek"] = _deepseek_factory


_register_anthropic()
_register_deepseek()


def get_provider(name: str) -> ProviderAdapter:
    """Resolve a provider name to its adapter instance.

    Raises ValueError if the name is not registered.
    """
    factory = _PROVIDERS.get(name.lower())
    if factory is None:
        available = ", ".join(sorted(_PROVIDERS.keys()))
        raise ValueError(
            f"Unknown provider '{name}'. Registered providers: {available}"
        )
    return factory()


def register_provider(
    name: str,
    factory: Callable[[], ProviderAdapter],
) -> None:
    """Register an additional provider at runtime (e.g. from plugins)."""
    _PROVIDERS[name.lower()] = factory


def list_providers() -> list[str]:
    """Return the currently registered provider names."""
    return sorted(_PROVIDERS.keys())


__all__ = [
    "ProviderAdapter",
    "get_provider",
    "register_provider",
    "list_providers",
]
