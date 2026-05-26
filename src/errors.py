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

from enum import Enum


class AgentErrorCode(Enum):
    """Unified error codes spanning API and tool layers."""

    # API layer
    API_CONNECTION_ERROR = "api_connection_error"
    API_TIMEOUT = "api_timeout"
    API_RATE_LIMIT = "api_rate_limit"        # 429
    API_OVERLOADED = "api_overloaded"         # 529
    API_AUTH_ERROR = "api_auth_error"         # 401/403
    API_BAD_REQUEST = "api_bad_request"       # 400
    API_SERVER_ERROR = "api_server_error"     # 5xx
    API_UNKNOWN = "api_unknown"

    # Tool layer
    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_EXEC_ERROR = "tool_exec_error"
    TOOL_MAP_ERROR = "tool_map_error"


# ---------------------------------------------------------------------------
# API error classification
# ---------------------------------------------------------------------------

def _classify_by_status(status: int | None) -> AgentErrorCode | None:
    """Map HTTP status code to error code (shared by all providers)."""
    if status is None:
        return None
    if status == 429:
        return AgentErrorCode.API_RATE_LIMIT
    if status == 529:
        return AgentErrorCode.API_OVERLOADED
    if status in (401, 403):
        return AgentErrorCode.API_AUTH_ERROR
    if status == 400:
        return AgentErrorCode.API_BAD_REQUEST
    if status >= 500:
        return AgentErrorCode.API_SERVER_ERROR
    return None


def classify_api_error(error: Exception) -> AgentErrorCode:
    """Map an SDK exception to an AgentErrorCode.

    Supports both Anthropic and OpenAI SDK exceptions.
    Falls back to keyword matching on str(error).
    """
    # --- Anthropic SDK ---
    try:
        from anthropic import APIError as AnthrAPIError, APIConnectionError as AnthrConnError
        if isinstance(error, AnthrConnError):
            error_str = str(error).lower()
            if "timeout" in error_str or "timed out" in error_str:
                return AgentErrorCode.API_TIMEOUT
            return AgentErrorCode.API_CONNECTION_ERROR
        if isinstance(error, AnthrAPIError):
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            return _classify_by_status(status) or AgentErrorCode.API_UNKNOWN
    except ImportError:
        pass

    # --- OpenAI SDK (DeepSeek, GPT, etc.) ---
    try:
        from openai import APIError as OaiAPIError, APIConnectionError as OaiConnError
        if isinstance(error, OaiConnError):
            error_str = str(error).lower()
            if "timeout" in error_str or "timed out" in error_str:
                return AgentErrorCode.API_TIMEOUT
            return AgentErrorCode.API_CONNECTION_ERROR
        if isinstance(error, OaiAPIError):
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            return _classify_by_status(status) or AgentErrorCode.API_UNKNOWN
    except ImportError:
        pass

    # --- Generic fallback ---
    error_str = str(error).lower()
    if "timeout" in error_str or "timed out" in error_str:
        return AgentErrorCode.API_TIMEOUT
    if "context_length_exceeded" in error_str or "prompt is too long" in error_str:
        return AgentErrorCode.API_BAD_REQUEST

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
    AgentErrorCode.API_SERVER_ERROR: (
        "The API returned a server error (5xx). "
        "This is a temporary issue — please try again."
    ),
    AgentErrorCode.API_UNKNOWN: (
        "An unexpected error occurred while calling the API."
    ),
}


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
