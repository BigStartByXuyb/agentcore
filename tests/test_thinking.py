"""Tests for Extended Thinking support.

Covers:
  - config defaults
  - _serialize_content() handling of thinking blocks
  - _build_thinking_dict() logic (Anthropic adapter)
  - classify_api_error → API_THINKING_ERROR detection
  - clean_thinking_history() in-place cleanup
  - ThinkingBlock event display via default_handler
  - token tracking with thinking tokens
  - 400 error recovery in run_agent_loop
"""

import types
import pytest
from unittest.mock import patch, MagicMock

from src.core import config
from src.core.types import (
    AgentState, Message, ThinkingContent, RedactedThinkingContent,
    TextContent, ToolUseContent,
)
from src.messages import clean_thinking_history as _clean_thinking_history
from src.agent_loop import (
    _serialize_content,
    run_agent_loop,
)
from src.core.errors import classify_api_error, AgentErrorCode
from src.display import default_handler
from src.core.events import ThinkingBlock


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
# _build_thinking_dict (Anthropic adapter level — returns API dict)
# ===================================================================

class TestBuildThinkingDict:
    def test_enabled(self):
        from src.providers.anthropic.adapter import _build_thinking_dict
        with patch.object(config, "THINKING_ENABLED", True), \
             patch.object(config, "THINKING_BUDGET_TOKENS", 10000):
            result = _build_thinking_dict(max_tokens=16384)
            assert result == {"type": "enabled", "budget_tokens": 10000}

    def test_disabled(self):
        from src.providers.anthropic.adapter import _build_thinking_dict
        with patch.object(config, "THINKING_ENABLED", False):
            assert _build_thinking_dict(max_tokens=16384) is None

    def test_budget_capped_to_max_tokens_minus_1(self):
        from src.providers.anthropic.adapter import _build_thinking_dict
        with patch.object(config, "THINKING_ENABLED", True), \
             patch.object(config, "THINKING_BUDGET_TOKENS", 20000):
            result = _build_thinking_dict(max_tokens=8000)
            assert result["budget_tokens"] == 7999

    def test_zero_budget_returns_none(self):
        from src.providers.anthropic.adapter import _build_thinking_dict
        with patch.object(config, "THINKING_ENABLED", True), \
             patch.object(config, "THINKING_BUDGET_TOKENS", 0):
            assert _build_thinking_dict(max_tokens=100) is None


# ===================================================================
# classify_api_error → API_THINKING_ERROR
# ===================================================================

class TestThinkingErrorClassification:
    def test_invalid_signature(self):
        from anthropic import APIError
        err = APIError(message="invalid signature in thinking block", request=MagicMock(), body=None)
        err.status_code = 400
        assert classify_api_error(err) == AgentErrorCode.API_THINKING_ERROR

    def test_thinking_cannot_be_modified(self):
        from anthropic import APIError
        err = APIError(message="thinking blocks cannot be modified", request=MagicMock(), body=None)
        err.status_code = 400
        assert classify_api_error(err) == AgentErrorCode.API_THINKING_ERROR

    def test_non_400_error(self):
        from anthropic import APIError
        err = APIError(message="invalid signature in thinking block", request=MagicMock(), body=None)
        err.status_code = 500
        assert classify_api_error(err) == AgentErrorCode.API_SERVER_ERROR

    def test_400_unrelated_message(self):
        from anthropic import APIError
        err = APIError(message="malformed request body", request=MagicMock(), body=None)
        err.status_code = 400
        assert classify_api_error(err) == AgentErrorCode.API_BAD_REQUEST

    def test_non_api_error(self):
        assert classify_api_error(ValueError("something")) == AgentErrorCode.API_UNKNOWN


# ===================================================================
# clean_thinking_history (in-place)
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
        from src.core.types import ToolUseContext, MessageHistory
        from src.providers.stream import StreamEvent
        from src.providers.types import ProviderMessage, TextBlock, Usage
        import asyncio

        call_count = 0

        async def mock_stream(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield ProviderMessage(
                    content=[TextBlock(text="invalid signature in thinking block")],
                    stop_reason="error",
                    is_error=True,
                    error_code=AgentErrorCode.API_THINKING_ERROR.value,
                )
            else:
                yield StreamEvent(type="text", text="recovered")
                yield ProviderMessage(
                    content=[TextBlock(text="recovered")],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=100, output_tokens=50),
                )

        history = MessageHistory([Message(role="user", content="test")])
        ctx = ToolUseContext(messages=history, tools=["bash"])

        with patch("src.agent_loop.query_model_stream", mock_stream):
            result = asyncio.run(run_agent_loop(
                system_prompt="test",
                tool_use_context=ctx,
                max_turns=3,
                thinking=True,
                on_event=lambda _: None,
            ))
        assert result == "recovered"
        assert call_count == 2

    def test_non_thinking_400_not_recovered(self):
        from src.core.types import ToolUseContext, MessageHistory
        from src.providers.types import ProviderMessage, TextBlock
        import asyncio

        async def mock_stream(**kwargs):
            yield ProviderMessage(
                content=[TextBlock(text="malformed request")],
                stop_reason="error",
                is_error=True,
                error_code=AgentErrorCode.API_BAD_REQUEST.value,
            )

        history = MessageHistory([Message(role="user", content="test")])
        ctx = ToolUseContext(messages=history, tools=["bash"])

        with patch("src.agent_loop.query_model_stream", mock_stream):
            result = asyncio.run(run_agent_loop(
                system_prompt="test",
                tool_use_context=ctx,
                max_turns=3,
                thinking=True,
                on_event=lambda _: None,
            ))
        assert "max turns" in result.lower()
