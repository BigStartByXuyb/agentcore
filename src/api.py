"""API dispatcher — routes calls to the active provider adapter.

Historically this file contained the Anthropic client + retry logic
directly.  Phase-0 of the multi-provider refactor moved that code into
src/providers/anthropic/adapter.py, and this module is now a **thin
shim** that preserves the public surface so existing callers
(agent_loop.py, memory/recall.py, tests) keep working unchanged.

All real work is done by the adapter returned by
`get_provider(config.PROVIDER)` — see src/providers/__init__.py for the
registry.

Public surface (unchanged from pre-refactor):
  - query_model(...)              → adapter.create_message(...)
  - create_stream_with_retry(...) → adapter.stream_message(...)
  - side_query(...)               → adapter.side_query(...)
  - get_client() / reset_client() → Anthropic client singleton access
                                    (kept for callers that still reach
                                    into the raw SDK; other providers
                                    won't expose a client here).
"""

from __future__ import annotations

from typing import Callable

import anthropic

from src import config
from src.providers import get_provider
from src.providers.anthropic import (
    get_default_adapter as _get_anthropic_adapter,
)

# Retry callback signature — (delay_seconds, attempt, max_attempts) -> None
_RetryCb = Callable[[float, int, int], None]


# ---------------------------------------------------------------------------
# Retry constants — re-exported so existing imports keep working.
# (Callers reference api.DEFAULT_MAX_RETRIES etc. via tests and docs.)
# ---------------------------------------------------------------------------
from src.providers.anthropic.adapter import (  # noqa: E402
    DEFAULT_MAX_RETRIES,
    BASE_DELAY_MS,
    MAX_DELAY_MS,
)


# ---------------------------------------------------------------------------
# Client access (Anthropic-specific, kept for raw SDK callers)
# ---------------------------------------------------------------------------

def get_client() -> anthropic.Anthropic:
    """Return the active Anthropic client.

    Only meaningful when config.PROVIDER == 'anthropic'.  Other providers
    don't expose a raw anthropic.Anthropic object.  Retained because some
    tests and legacy call sites reach directly into the SDK.
    """
    return _get_anthropic_adapter().get_client()


def reset_client() -> None:
    """Force re-creation of the Anthropic client on next call."""
    _get_anthropic_adapter().reset_client()


# ---------------------------------------------------------------------------
# Dispatchers — forward to the active provider adapter
# ---------------------------------------------------------------------------

def query_model(
    *,
    messages: list[dict],
    system: str,
    tools: list[dict],
    model: str | None = None,
    max_tokens: int | None = None,
    thinking: dict | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    on_retry: _RetryCb | None = None,
) -> anthropic.types.Message:
    """Call the active provider's create_message."""
    adapter = get_provider(config.PROVIDER)
    return adapter.create_message(
        messages=messages,
        system=system,
        tools=tools,
        model=model,
        max_tokens=max_tokens,
        thinking=thinking,
        max_retries=max_retries,
        on_retry=on_retry,
    )


def create_stream_with_retry(
    *,
    messages: list[dict],
    system: str,
    tools: list[dict],
    model: str | None = None,
    max_tokens: int | None = None,
    thinking: dict | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    on_retry: _RetryCb | None = None,
):
    """Call the active provider's stream_message.

    Returns the provider's stream context manager; used inside `with`
    to iterate text_stream and call get_final_message().
    """
    adapter = get_provider(config.PROVIDER)
    return adapter.stream_message(
        messages=messages,
        system=system,
        tools=tools,
        model=model,
        max_tokens=max_tokens,
        thinking=thinking,
        max_retries=max_retries,
        on_retry=on_retry,
    )


def side_query(
    *,
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int = 256,
    output_format: dict | None = None,
) -> anthropic.types.Message:
    """Call the active provider's side_query."""
    adapter = get_provider(config.PROVIDER)
    return adapter.side_query(
        model=model,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        output_format=output_format,
    )


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "BASE_DELAY_MS",
    "MAX_DELAY_MS",
    "get_client",
    "reset_client",
    "query_model",
    "create_stream_with_retry",
    "side_query",
]
