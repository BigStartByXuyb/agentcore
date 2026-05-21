"""Tests for Extended Thinking support.

Covers:
  - config defaults
  - _serialize_content() handling of thinking blocks
  - _build_thinking_param() logic
  - _is_thinking_400() detection
  - _clean_thinking_history() in-place cleanup
  - strip_thinking_blocks() / filter_orphaned_thinking_messages()
  - ThinkingBlock event display via default_handler
  - token tracking with thinking tokens
  - 400 error recovery in run_agent_loop
"""

import types
import pytest
from unittest.mock import patch, MagicMock

from src import config
from src.types import (
    AgentState, Message, ThinkingContent, RedactedThinkingContent,
    TextContent, ToolUseContent,
)
from src.messages import (
    strip_thinking_blocks,
    filter_orphaned_thinking_messages,
    _is_thinking_block,
)
from src.agent_loop import (
    _serialize_content,
    _build_thinking_param,
    _is_thinking_400,
    _clean_thinking_history,
    run_agent_loop,
)
from src.display import default_handler
from src.events import ThinkingBlock


# ===================================================================
# Config defaults
# ===================================================================

class TestConfig:
    def test_thinking_enabled_default(self):
        assert config.THINKING_ENABLED is True

    def test_thinking_budget_default(self):
        assert config.THINKING_BUDGET_TOKENS == 10000

    def test_budget_less_than_max_tokens(self):
        assert config.THINKING_BUDGET_TOKENS < config.MAX_TOKENS


# ===================================================================
# _is_thinking_block helper
# ===================================================================

class TestIsThinkingBlock:
    def test_thinking_block(self):
        assert _is_thinking_block({"type": "thinking", "thinking": "...", "signature": "sig"}) is True

    def test_redacted_thinking_block(self):
        assert _is_thinking_block({"type": "redacted_thinking", "data": "..."}) is True

    def test_text_block(self):
        assert _is_thinking_block({"type": "text", "text": "hello"}) is False

    def test_tool_use_block(self):
        assert _is_thinking_block({"type": "tool_use", "id": "1", "name": "bash", "input": {}}) is False


# ===================================================================
# _serialize_content — thinking blocks
# ===================================================================

class TestSerializeThinking:
    def _make_block(self, **kwargs):
        """Create a SimpleNamespace mimicking an SDK content block."""
        return types.SimpleNamespace(**kwargs)

    def test_thinking_block_serialized(self):
        blocks = [self._make_block(type="thinking", thinking="my thought", signature="sig123")]
        result = _serialize_content(blocks)
        assert len(result) == 1
        assert isinstance(result[0], ThinkingContent)
        assert result[0].thinking == "my thought"
        assert result[0].signature == "sig123"

    def test_redacted_thinking_block_serialized(self):
        blocks = [self._make_block(type="redacted_thinking", data="redacted_data")]
        result = _serialize_content(blocks)
        assert len(result) == 1
        assert isinstance(result[0], RedactedThinkingContent)
        assert result[0].data == "redacted_data"

    def test_mixed_blocks(self):
        blocks = [
            self._make_block(type="thinking", thinking="thought", signature="s1"),
            self._make_block(type="text", text="hello"),
            self._make_block(type="tool_use", id="t1", name="bash", input={"command": "ls"}),
        ]
        result = _serialize_content(blocks)
        assert len(result) == 3
        assert result[0].type == "thinking"
        assert result[1].type == "text"
        assert result[2].type == "tool_use"

    def test_unknown_block_type_skipped(self):
        blocks = [self._make_block(type="unknown_type", data="x")]
        result = _serialize_content(blocks)
        assert result == []


# ===================================================================
# _build_thinking_param
# ===================================================================

class TestBuildThinkingParam:
    def test_enabled(self):
        with patch.object(config, "THINKING_ENABLED", True), \
             patch.object(config, "THINKING_BUDGET_TOKENS", 10000), \
             patch.object(config, "MAX_TOKENS", 16384):
            result = _build_thinking_param()
            assert result == {"type": "enabled", "budget_tokens": 10000}

    def test_disabled(self):
        with patch.object(config, "THINKING_ENABLED", False):
            assert _build_thinking_param() is None

    def test_budget_capped_to_max_tokens_minus_1(self):
        with patch.object(config, "THINKING_ENABLED", True), \
             patch.object(config, "THINKING_BUDGET_TOKENS", 20000), \
             patch.object(config, "MAX_TOKENS", 8000):
            result = _build_thinking_param()
            assert result["budget_tokens"] == 7999

    def test_zero_budget_returns_none(self):
        with patch.object(config, "THINKING_ENABLED", True), \
             patch.object(config, "THINKING_BUDGET_TOKENS", 0), \
             patch.object(config, "MAX_TOKENS", 100):
            assert _build_thinking_param() is None


# ===================================================================
# _is_thinking_400
# ===================================================================

class TestIsThinking400:
    def test_invalid_signature(self):
        from anthropic import APIError
        err = APIError(message="invalid signature in thinking block", request=MagicMock(), body=None)
        err.status_code = 400
        assert _is_thinking_400(err) is True

    def test_thinking_cannot_be_modified(self):
        from anthropic import APIError
        err = APIError(message="thinking blocks cannot be modified", request=MagicMock(), body=None)
        err.status_code = 400
        assert _is_thinking_400(err) is True

    def test_non_400_error(self):
        from anthropic import APIError
        err = APIError(message="invalid signature in thinking block", request=MagicMock(), body=None)
        err.status_code = 500
        assert _is_thinking_400(err) is False

    def test_400_unrelated_message(self):
        from anthropic import APIError
        err = APIError(message="malformed request body", request=MagicMock(), body=None)
        err.status_code = 400
        assert _is_thinking_400(err) is False

    def test_non_api_error(self):
        assert _is_thinking_400(ValueError("something")) is False


# ===================================================================
# strip_thinking_blocks
# ===================================================================

class TestStripThinkingBlocks:
    def test_strips_thinking_from_assistant(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "s"},
                {"type": "text", "text": "hello"},
            ]},
        ]
        result = strip_thinking_blocks(messages)
        assert len(result) == 1
        assert len(result[0]["content"]) == 1
        assert result[0]["content"][0]["type"] == "text"

    def test_strips_redacted_thinking(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "redacted_thinking", "data": "x"},
                {"type": "text", "text": "hi"},
            ]},
        ]
        result = strip_thinking_blocks(messages)
        assert result[0]["content"] == [{"type": "text", "text": "hi"}]

    def test_leaves_user_messages_untouched(self):
        messages = [{"role": "user", "content": "hello"}]
        result = strip_thinking_blocks(messages)
        assert result is messages  # same object — no change

    def test_no_thinking_returns_same_list(self):
        messages = [
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        result = strip_thinking_blocks(messages)
        assert result is messages

    def test_all_thinking_stripped_to_empty(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "t", "signature": "s"},
            ]},
        ]
        result = strip_thinking_blocks(messages)
        assert result[0]["content"] == []


# ===================================================================
# filter_orphaned_thinking_messages
# ===================================================================

class TestFilterOrphanedThinkingMessages:
    def test_removes_thinking_only_assistant(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "t", "signature": "s"},
            ]},
            {"role": "user", "content": "next"},
        ]
        result = filter_orphaned_thinking_messages(messages)
        assert len(result) == 2
        assert all(m["role"] == "user" for m in result)

    def test_keeps_mixed_assistant(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "t", "signature": "s"},
                {"type": "text", "text": "hello"},
            ]},
        ]
        result = filter_orphaned_thinking_messages(messages)
        assert len(result) == 1

    def test_removes_empty_content_assistant(self):
        messages = [{"role": "assistant", "content": []}]
        result = filter_orphaned_thinking_messages(messages)
        assert len(result) == 0

    def test_keeps_non_list_content(self):
        messages = [{"role": "assistant", "content": "text"}]
        result = filter_orphaned_thinking_messages(messages)
        assert len(result) == 1


# ===================================================================
# _clean_thinking_history (in-place)
# ===================================================================

class TestCleanThinkingHistory:
    def test_in_place_mutation(self):
        messages = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[
                ThinkingContent(thinking="t", signature="s"),
            ]),
            Message(role="assistant", content=[
                ThinkingContent(thinking="t2", signature="s2"),
                TextContent(text="hello"),
            ]),
        ]
        original_id = id(messages)
        _clean_thinking_history(messages)
        assert id(messages) == original_id
        assert len(messages) == 2
        assert len(messages[1].content) == 1
        assert isinstance(messages[1].content[0], TextContent)
        assert messages[1].content[0].text == "hello"


# ===================================================================
# ThinkingBlock event display via default_handler
# ===================================================================

class TestThinkingDisplay:
    def test_prints_thinking(self, capsys):
        event = ThinkingBlock(label="main", thinking="my deep thought")
        default_handler(event)
        captured = capsys.readouterr()
        assert "my deep thought" in captured.out
        assert "[main:thinking]" in captured.out

    def test_truncates_long_thinking(self, capsys):
        long_text = "x" * 300
        event = ThinkingBlock(label="test", thinking=long_text)
        default_handler(event)
        captured = capsys.readouterr()
        assert "..." in captured.out
        assert len(captured.out.split("]")[1].strip()) < 300

    def test_skips_empty_thinking(self, capsys):
        event = ThinkingBlock(label="main", thinking="")
        default_handler(event)
        captured = capsys.readouterr()
        assert captured.out == ""


# ===================================================================
# AgentState thinking tokens
# ===================================================================

class TestAgentStateThinkingTokens:
    def test_default_zero(self):
        state = AgentState()
        assert state.total_thinking_tokens == 0

    def test_accumulates(self):
        state = AgentState()
        state.total_thinking_tokens += 500
        state.total_thinking_tokens += 300
        assert state.total_thinking_tokens == 800


# ===================================================================
# run_agent_loop — thinking 400 recovery
# ===================================================================

class TestThinkingRecovery:
    """Test that run_agent_loop strips thinking blocks on thinking-related 400."""

    def test_thinking_400_triggers_strip_and_retry(self):
        from unittest.mock import AsyncMock
        from anthropic import APIError
        from src.types import ToolUseContext, MessageHistory
        import asyncio

        err = APIError(message="invalid signature in thinking block", request=MagicMock(), body=None)
        err.status_code = 400

        success_response = MagicMock()
        success_response.usage.input_tokens = 100
        success_response.usage.output_tokens = 50
        success_response.usage.cache_creation_input_tokens = 0
        success_response.stop_reason = "end_turn"
        success_response.content = [types.SimpleNamespace(type="text", text="recovered")]

        mock_query = AsyncMock(side_effect=[err, success_response])

        history = MessageHistory([Message(role="user", content="test")])
        ctx = ToolUseContext(messages=history, tools=["bash"])

        with patch("src.agent_loop.query_model", mock_query):
            result = asyncio.run(run_agent_loop(
                system_prompt="test",
                tool_use_context=ctx,
                max_turns=3,
                thinking={"type": "enabled", "budget_tokens": 5000},
                on_event=lambda _: None,
            ))
        assert result == "recovered"
        assert mock_query.call_count == 2

    def test_non_thinking_400_not_recovered(self):
        from unittest.mock import AsyncMock
        from anthropic import APIError
        from src.types import ToolUseContext, MessageHistory
        import asyncio

        err = APIError(message="malformed request", request=MagicMock(), body=None)
        err.status_code = 400

        mock_query = AsyncMock(side_effect=err)

        history = MessageHistory([Message(role="user", content="test")])
        ctx = ToolUseContext(messages=history, tools=["bash"])

        with patch("src.agent_loop.query_model", mock_query):
            result = asyncio.run(run_agent_loop(
                system_prompt="test",
                tool_use_context=ctx,
                max_turns=3,
                thinking={"type": "enabled", "budget_tokens": 5000},
                on_event=lambda _: None,
            ))
        assert "max turns" in result.lower()
