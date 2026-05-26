"""DeepSeek format converter — bidirectional translation between internal and OpenAI formats.

Request direction (agent_loop → DeepSeek API):
  list[Message] (from prepare_messages()) → OpenAI chat completion format

Response direction (DeepSeek API → agent_loop):
  OpenAI ChatCompletion response → ProviderMessage (unified types)

All format translation is isolated here; the adapter calls these helpers
and never exposes OpenAI-format details to the rest of the codebase.
"""

from __future__ import annotations

import json
from typing import Any

from src.types import (
    Message,
    ContentBlock,
    TextContent,
    ToolUseContent,
    ToolResultContent,
    ThinkingContent,
    RedactedThinkingContent,
)
from src.providers.types import (
    ProviderMessage,
    TextBlock,
    ToolUseBlock,
    ThinkingBlock,
    Usage,
)


# ---------------------------------------------------------------------------
# Request direction: list[Message] → OpenAI format
# ---------------------------------------------------------------------------

def system_to_openai(system: str) -> dict:
    """Convert the system prompt to an OpenAI system message."""
    return {"role": "system", "content": system}


def tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert tool schemas to OpenAI function-calling format.

    Input:  {"name": "bash", "description": "...", "input_schema": {...}}
    Output: {"type": "function", "function": {"name": "bash", ...}}
    """
    result: list[dict] = []
    for tool in tools:
        func: dict[str, Any] = {"name": tool["name"]}
        if "description" in tool:
            func["description"] = tool["description"]
        if "input_schema" in tool:
            func["parameters"] = tool["input_schema"]
        result.append({"type": "function", "function": func})
    return result


def messages_to_openai(messages: list[Message], system: str) -> list[dict]:
    """Convert prepared Message objects to OpenAI message format.

    Handles:
      - system prompt → {"role": "system", ...} at front
      - user messages with text content → flatten to string
      - user messages with tool_result blocks → split into role:"tool" messages
      - assistant messages with tool_use blocks → convert to tool_calls array
      - thinking blocks → skipped (OpenAI has no thinking input)
    """
    result: list[dict] = []

    if system:
        result.append(system_to_openai(system))

    for msg in messages:
        if msg.role == "user":
            result.extend(_convert_user_message(msg.content))
        elif msg.role == "assistant":
            result.append(_convert_assistant_message(msg.content))
        else:
            raise ValueError(f"Unsupported message role: {msg.role!r}")

    return result


def _convert_user_message(content: str | list[ContentBlock]) -> list[dict]:
    """Convert a user message's content to OpenAI format.

    A single user message may contain mixed text + tool_result blocks.
    OpenAI requires tool results as separate {"role": "tool"} messages.
    """
    if isinstance(content, str):
        return [{"role": "user", "content": content}]

    text_parts: list[str] = []
    tool_results: list[dict] = []

    for block in content:
        if isinstance(block, TextContent):
            text_parts.append(block.text)
        elif isinstance(block, ToolResultContent):
            tool_results.append({
                "role": "tool",
                "tool_call_id": block.tool_use_id,
                "content": block.content,
            })

    result: list[dict] = []
    if text_parts:
        result.append({"role": "user", "content": "\n".join(text_parts)})
    result.extend(tool_results)
    return result


def _convert_assistant_message(content: str | list[ContentBlock]) -> dict:
    """Convert an assistant message's content to OpenAI format.

    Text blocks → concatenated into content string.
    ToolUse blocks → converted to tool_calls array.
    Thinking blocks → skipped (no OpenAI equivalent for input).
    """
    if isinstance(content, str):
        return {"role": "assistant", "content": content}

    text_parts: list[str] = []
    tool_calls: list[dict] = []

    for block in content:
        if isinstance(block, TextContent):
            text_parts.append(block.text)
        elif isinstance(block, ToolUseContent):
            tool_calls.append({
                "id": block.id,
                "type": "function",
                "function": {
                    "name": block.name,
                    "arguments": json.dumps(block.input, ensure_ascii=False),
                },
            })
        elif isinstance(block, (ThinkingContent, RedactedThinkingContent)):
            continue

    msg: dict[str, Any] = {"role": "assistant"}
    msg["content"] = "\n".join(text_parts) if text_parts else None
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


# ---------------------------------------------------------------------------
# Response direction: OpenAI format → ProviderMessage
# ---------------------------------------------------------------------------

def response_to_provider(response: Any) -> ProviderMessage:
    """Convert an OpenAI ChatCompletion response to ProviderMessage."""
    choice = response.choices[0]
    message = choice.message

    content_blocks: list[TextBlock | ToolUseBlock | ThinkingBlock] = []

    reasoning = getattr(message, "reasoning_content", None)
    if reasoning:
        content_blocks.append(ThinkingBlock(thinking=reasoning, signature=""))

    if message.content:
        content_blocks.append(TextBlock(text=message.content))

    if message.tool_calls:
        for tc in message.tool_calls:
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            content_blocks.append(ToolUseBlock(
                id=tc.id,
                name=tc.function.name,
                input=args,
            ))

    stop_reason = _map_finish_reason(choice.finish_reason)

    usage = Usage()
    if response.usage:
        usage = Usage(
            input_tokens=response.usage.prompt_tokens or 0,
            output_tokens=response.usage.completion_tokens or 0,
        )

    return ProviderMessage(
        content=content_blocks,
        stop_reason=stop_reason,
        usage=usage,
    )


def _map_finish_reason(reason: str | None) -> str:
    """Map OpenAI finish_reason to our unified stop_reason."""
    if reason is None:
        return "end_turn"
    mapping = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "content_filter": "end_turn",
    }
    return mapping.get(reason, "end_turn")
