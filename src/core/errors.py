"""Error classification and synthetic message generation.

Corresponds to Claude Code's:
  - src/errors.ts:425 — getAssistantMessageFromError()
  - src/query.ts:955  — outer try/catch converting errors to assistant messages

Two core responsibilities:
  1. classify_api_error()  — map SDK exceptions to AgentErrorCode
  2. create_assistant_error_message() — build a synthetic assistant message
     so the message history stays user/assistant alternation-correct even
     when the API call itself fails.
"""

from __future__ import annotations

import re
from enum import Enum


class AgentErrorCode(Enum):
    """Unified error codes spanning API and tool layers."""

    # API layer
    API_CONNECTION_ERROR = "api_connection_error"
    API_TIMEOUT = "api_timeout"
    API_RATE_LIMIT = "api_rate_limit"        # 429
    API_OVERLOADED = "api_overloaded"         # 529
    API_AUTH_ERROR = "api_auth_error"         # 401/403
    API_BAD_REQUEST = "api_bad_request"       # 400 (generic)
    API_PROMPT_TOO_LONG = "api_prompt_too_long"  # 400/413 with PTL keywords
    API_THINKING_ERROR = "api_thinking_error"    # 400 with thinking-block keywords
    API_SERVER_ERROR = "api_server_error"     # 5xx
    API_UNKNOWN = "api_unknown"

    # Tool layer
    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_EXEC_ERROR = "tool_exec_error"
    TOOL_MAP_ERROR = "tool_map_error"


# ---------------------------------------------------------------------------
# API error classification
# ---------------------------------------------------------------------------

_PTL_KEYWORDS = (
    "prompt is too long", "prompt_too_long",
    "context_length_exceeded", "maximum context length",
    "too many tokens",
)
_THINKING_KEYWORDS = ("invalid signature", "thinking blocks cannot be modified")


def _classify_by_status(status: int | None, error_msg: str = "") -> AgentErrorCode | None:
    """Map HTTP status code + error message to error code (shared by all providers)."""
    if status is None:
        return None
    if status == 429:
        return AgentErrorCode.API_RATE_LIMIT
    if status == 529:
        return AgentErrorCode.API_OVERLOADED
    if status in (401, 403):
        return AgentErrorCode.API_AUTH_ERROR
    if status == 413:
        return AgentErrorCode.API_PROMPT_TOO_LONG
    if status == 400:
        msg = error_msg.lower()
        if any(kw in msg for kw in _PTL_KEYWORDS):
            return AgentErrorCode.API_PROMPT_TOO_LONG
        if any(kw in msg for kw in _THINKING_KEYWORDS):
            return AgentErrorCode.API_THINKING_ERROR
        return AgentErrorCode.API_BAD_REQUEST
    if status >= 500:
        return AgentErrorCode.API_SERVER_ERROR
    return None


def classify_api_error(error: Exception) -> AgentErrorCode:
    """Map an SDK exception to an AgentErrorCode.

    Supports both Anthropic and OpenAI SDK exceptions.
    Falls back to keyword matching on str(error).
    """
    error_msg = str(error)

    # --- Anthropic SDK ---
    try:
        from anthropic import APIError as AnthrAPIError, APIConnectionError as AnthrConnError
        if isinstance(error, AnthrConnError):
            msg_lower = error_msg.lower()
            if "timeout" in msg_lower or "timed out" in msg_lower:
                return AgentErrorCode.API_TIMEOUT
            return AgentErrorCode.API_CONNECTION_ERROR
        if isinstance(error, AnthrAPIError):
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            return _classify_by_status(status, error_msg) or AgentErrorCode.API_UNKNOWN
    except ImportError:
        pass

    # --- OpenAI SDK (DeepSeek, GPT, etc.) ---
    try:
        from openai import APIError as OaiAPIError, APIConnectionError as OaiConnError
        if isinstance(error, OaiConnError):
            msg_lower = error_msg.lower()
            if "timeout" in msg_lower or "timed out" in msg_lower:
                return AgentErrorCode.API_TIMEOUT
            return AgentErrorCode.API_CONNECTION_ERROR
        if isinstance(error, OaiAPIError):
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            return _classify_by_status(status, error_msg) or AgentErrorCode.API_UNKNOWN
    except ImportError:
        pass

    # --- Generic fallback (no SDK exception type matched) ---
    msg_lower = error_msg.lower()
    if "timeout" in msg_lower or "timed out" in msg_lower:
        return AgentErrorCode.API_TIMEOUT
    if any(kw in msg_lower for kw in _PTL_KEYWORDS):
        return AgentErrorCode.API_PROMPT_TOO_LONG

    return AgentErrorCode.API_UNKNOWN


# ---------------------------------------------------------------------------
# Error messages per code
# ---------------------------------------------------------------------------

_ERROR_MESSAGES: dict[AgentErrorCode, str] = {
    AgentErrorCode.API_CONNECTION_ERROR: (
        "I couldn't connect to the API server. "
        "Please check your network connection and ANTHROPIC_BASE_URL setting."
    ),
    AgentErrorCode.API_TIMEOUT: (
        "The API request timed out. "
        "The server may be under heavy load — please try again in a moment."
    ),
    AgentErrorCode.API_RATE_LIMIT: (
        "Rate limit exceeded (429). "
        "Too many requests — please wait a moment before retrying."
    ),
    AgentErrorCode.API_OVERLOADED: (
        "The API is temporarily overloaded (529). "
        "Please try again in a few seconds."
    ),
    AgentErrorCode.API_AUTH_ERROR: (
        "Authentication failed. "
        "Please check your ANTHROPIC_AUTH_TOKEN is valid."
    ),
    AgentErrorCode.API_BAD_REQUEST: (
        "The API rejected the request (400 Bad Request). "
        "This usually indicates a malformed request — "
        "the conversation context may need to be reset."
    ),
    AgentErrorCode.API_PROMPT_TOO_LONG: (
        "The prompt is too long and exceeds the model's context window. "
        "The conversation will be compacted automatically."
    ),
    AgentErrorCode.API_THINKING_ERROR: (
        "The API returned a thinking-related error (400). "
        "Thinking blocks will be stripped and the request retried."
    ),
    AgentErrorCode.API_SERVER_ERROR: (
        "The API returned a server error (5xx). "
        "This is a temporary issue — please try again."
    ),
    AgentErrorCode.API_UNKNOWN: (
        "An unexpected error occurred while calling the API."
    ),
}


def is_prompt_too_long(error: Exception) -> bool:
    """Detect prompt-too-long errors from any supported provider.

    Delegates to classify_api_error for unified classification.
    Kept as a convenience for callers that work with raw exceptions (e.g. auto_compact).
    """
    return classify_api_error(error) == AgentErrorCode.API_PROMPT_TOO_LONG


# Token extraction from PTL errors — keep in sync with _PTL_KEYWORDS above
_PTL_TOKEN_PATTERNS = [
    # Anthropic: "prompt is too long: 200000 tokens > 128000"
    re.compile(r'prompt is too long[^0-9]*(\d+)\s*tokens?\s*>\s*(\d+)', re.IGNORECASE),
    # OpenAI/DeepSeek: "maximum context length is 65536 tokens. However, your messages resulted in 70000 tokens"
    re.compile(r'maximum context length is (\d+) tokens.*?resulted in (\d+) tokens', re.IGNORECASE),
]


def parse_ptl_token_counts(error_message: str) -> tuple[int | None, int | None]:
    """Parse actual/limit token counts from a prompt-too-long error message.

    Supports multiple provider formats:
      - Anthropic: "prompt is too long: 200000 tokens > 128000"
        → (actual=200000, limit=128000)
      - OpenAI/DeepSeek: "maximum context length is 65536 tokens. However, your messages resulted in 70000 tokens"
        → (actual=70000, limit=65536)

    Returns (actual_tokens, limit_tokens) or (None, None) if unparseable.
    """
    for pattern in _PTL_TOKEN_PATTERNS:
        match = pattern.search(error_message)
        if match:
            a, b = int(match.group(1)), int(match.group(2))
            # Anthropic: group(1)=actual, group(2)=limit
            # OpenAI:    group(1)=limit, group(2)=actual
            return (a, b) if a > b else (b, a)
    return None, None


def get_ptl_token_gap(error: Exception | str | None) -> int | None:
    """Return how many tokens over the limit a PTL error reports.

    Accepts an Exception, a raw error message string, or None.
    Returns the gap (actual - limit) if parseable, None otherwise.
    """
    if error is None:
        return None
    actual, limit = parse_ptl_token_counts(str(error))
    if actual is None or limit is None:
        return None
    gap = actual - limit
    return gap if gap > 0 else None


def create_error_text(error: Exception) -> str:
    """Build a human-readable error description from an exception.

    Used by api.py to populate ProviderMessage(is_error=True) content.
    """
    code = classify_api_error(error)
    base_msg = _ERROR_MESSAGES.get(code, _ERROR_MESSAGES[AgentErrorCode.API_UNKNOWN])
    return f"{base_msg}\n\nError details: {type(error).__name__}: {error}"


def create_assistant_error_message(error: Exception) -> dict:
    """Build a synthetic assistant message from an error.

    Returns a dict ready to be appended to the message history, keeping
    user/assistant alternation intact even when query_model() fails.

    Format mirrors Claude Code's getAssistantMessageFromError():
      {"role": "assistant", "content": [{"type": "text", "text": "..."}]}
    """
    code = classify_api_error(error)
    base_msg = _ERROR_MESSAGES.get(code, _ERROR_MESSAGES[AgentErrorCode.API_UNKNOWN])

    # Append the raw error for debugging
    text = f"{base_msg}\n\nError details: {type(error).__name__}: {error}"

    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
    }
