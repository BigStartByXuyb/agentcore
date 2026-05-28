"""Tests for pre-send message normalization in prepare_messages().

Covers:
  1. _filter_orphaned_thinking_messages — drop thinking-only assistants
  2. _strip_trailing_thinking — remove trailing thinking from last assistant
  3. _ensure_tool_result_pairing — synthetic error for missing tool_results
  4. _filter_empty_assistants — drop empty/whitespace-only assistants
  5. _merge_adjacent_same_role — re-merge after filtering
  6. Full pipeline via prepare_messages()
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.types import (
    Message, MessageHistory,
    TextContent, ToolUseContent, ToolResultContent,
    ThinkingContent, RedactedThinkingContent,
    _filter_orphaned_thinking_messages,
    _strip_trailing_thinking,
    _ensure_tool_result_pairing,
    _filter_empty_assistants,
    _merge_adjacent_same_role,
    _normalize_for_api,
)


# ===================================================================
# 1. _filter_orphaned_thinking_messages
# ===================================================================

class TestFilterOrphanedThinking:
    def test_keeps_assistant_with_text_and_thinking(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[
                ThinkingContent(thinking="hmm", signature="sig1"),
                TextContent(text="hello"),
            ]),
        ]
        result = _filter_orphaned_thinking_messages(msgs)
        assert len(result) == 2

    def test_drops_thinking_only_assistant(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[
                ThinkingContent(thinking="hmm", signature="sig1"),
            ]),
            Message(role="user", content="hello again"),
            Message(role="assistant", content=[TextContent(text="ok")]),
        ]
        result = _filter_orphaned_thinking_messages(msgs)
        assert len(result) == 3
        assert all(
            m.role != "assistant" or any(
                not isinstance(b, (ThinkingContent, RedactedThinkingContent))
                for b in (m.content if isinstance(m.content, list) else [])
            )
            for m in result
            if m.role == "assistant"
        )

    def test_drops_redacted_thinking_only(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[
                RedactedThinkingContent(data="redacted"),
            ]),
        ]
        result = _filter_orphaned_thinking_messages(msgs)
        assert len(result) == 1
        assert result[0].role == "user"

    def test_keeps_user_messages_untouched(self):
        msgs = [Message(role="user", content="test")]
        result = _filter_orphaned_thinking_messages(msgs)
        assert len(result) == 1

    def test_drops_empty_assistant(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[]),
        ]
        result = _filter_orphaned_thinking_messages(msgs)
        assert len(result) == 1
        assert result[0].role == "user"


# ===================================================================
# 2. _strip_trailing_thinking
# ===================================================================

class TestStripTrailingThinking:
    def test_strips_trailing_thinking(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[
                TextContent(text="answer"),
                ThinkingContent(thinking="thought", signature="sig"),
            ]),
        ]
        result = _strip_trailing_thinking(msgs)
        assert len(result) == 2
        blocks = result[-1].content
        assert len(blocks) == 1
        assert blocks[0].type == "text"

    def test_strips_multiple_trailing_thinking(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[
                TextContent(text="answer"),
                ThinkingContent(thinking="t1", signature="s1"),
                RedactedThinkingContent(data="r1"),
            ]),
        ]
        result = _strip_trailing_thinking(msgs)
        blocks = result[-1].content
        assert len(blocks) == 1
        assert blocks[0].type == "text"

    def test_no_strip_if_not_trailing(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[
                ThinkingContent(thinking="thought", signature="sig"),
                TextContent(text="answer"),
            ]),
        ]
        result = _strip_trailing_thinking(msgs)
        assert len(result[-1].content) == 2

    def test_removes_message_if_all_thinking(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[
                ThinkingContent(thinking="thought", signature="sig"),
            ]),
        ]
        result = _strip_trailing_thinking(msgs)
        assert len(result) == 1

    def test_noop_if_last_is_user(self):
        msgs = [
            Message(role="assistant", content=[TextContent(text="hi")]),
            Message(role="user", content="bye"),
        ]
        result = _strip_trailing_thinking(msgs)
        assert len(result) == 2

    def test_empty_messages(self):
        assert _strip_trailing_thinking([]) == []


# ===================================================================
# 3. _ensure_tool_result_pairing
# ===================================================================

class TestEnsureToolResultPairing:
    def test_no_change_when_paired(self):
        msgs = [
            Message(role="user", content="do something"),
            Message(role="assistant", content=[
                ToolUseContent(id="t1", name="bash", input={}),
            ]),
            Message(role="user", content=[
                ToolResultContent(tool_use_id="t1", content="ok"),
            ]),
        ]
        result = _ensure_tool_result_pairing(msgs)
        user_blocks = result[2].content
        assert len(user_blocks) == 1

    def test_adds_missing_tool_result(self):
        msgs = [
            Message(role="user", content="do something"),
            Message(role="assistant", content=[
                ToolUseContent(id="t1", name="bash", input={}),
                ToolUseContent(id="t2", name="read_file", input={}),
            ]),
            Message(role="user", content=[
                ToolResultContent(tool_use_id="t1", content="ok"),
            ]),
        ]
        result = _ensure_tool_result_pairing(msgs)
        user_blocks = result[2].content
        assert len(user_blocks) == 2
        orphan = [b for b in user_blocks if isinstance(b, ToolResultContent) and b.tool_use_id == "t2"]
        assert len(orphan) == 1
        assert orphan[0].is_error is True

    def test_no_tool_use_no_change(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[TextContent(text="hello")]),
            Message(role="user", content="bye"),
        ]
        result = _ensure_tool_result_pairing(msgs)
        assert len(result) == 3

    def test_handles_multiple_pairs(self):
        msgs = [
            Message(role="user", content="a"),
            Message(role="assistant", content=[
                ToolUseContent(id="t1", name="bash", input={}),
            ]),
            Message(role="user", content=[
                ToolResultContent(tool_use_id="t1", content="ok"),
            ]),
            Message(role="assistant", content=[
                ToolUseContent(id="t2", name="bash", input={}),
            ]),
            Message(role="user", content=[]),  # missing t2 result
        ]
        result = _ensure_tool_result_pairing(msgs)
        last_user_blocks = result[4].content
        assert len(last_user_blocks) == 1
        assert last_user_blocks[0].tool_use_id == "t2"
        assert last_user_blocks[0].is_error is True


# ===================================================================
# 4. _filter_empty_assistants
# ===================================================================

class TestFilterEmptyAssistants:
    def test_removes_empty_content(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[]),
            Message(role="user", content="bye"),
        ]
        result = _filter_empty_assistants(msgs)
        assert len(result) == 2

    def test_removes_whitespace_only(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[TextContent(text="   \n\t  ")]),
            Message(role="user", content="bye"),
        ]
        result = _filter_empty_assistants(msgs)
        assert len(result) == 2

    def test_keeps_non_text_blocks(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[
                ToolUseContent(id="t1", name="bash", input={}),
            ]),
        ]
        result = _filter_empty_assistants(msgs)
        assert len(result) == 2

    def test_keeps_user_messages(self):
        msgs = [Message(role="user", content="")]
        result = _filter_empty_assistants(msgs)
        assert len(result) == 1


# ===================================================================
# 5. _merge_adjacent_same_role
# ===================================================================

class TestMergeAdjacentSameRole:
    def test_merges_consecutive_users(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="user", content="there"),
        ]
        result = _merge_adjacent_same_role(msgs)
        assert len(result) == 1

    def test_no_merge_alternating(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=[TextContent(text="hello")]),
        ]
        result = _merge_adjacent_same_role(msgs)
        assert len(result) == 2

    def test_empty_input(self):
        assert _merge_adjacent_same_role([]) == []


# ===================================================================
# 6. Full pipeline via prepare_messages()
# ===================================================================

class TestPrepareMessagesNormalize:
    def test_drops_orphaned_thinking_and_remerges(self):
        """Orphaned thinking removal + re-merge of now-adjacent same-role."""
        h = MessageHistory([
            Message(role="user", content="q1"),
            Message(role="assistant", content=[
                ThinkingContent(thinking="stale", signature="bad"),
            ]),
            Message(role="user", content="q2"),
            Message(role="assistant", content=[TextContent(text="a2")]),
        ])
        result = h.prepare_messages()
        # Thinking-only assistant dropped → two users merge → [user, assistant]
        assert len(result) == 2
        assert result[0].role == "user"
        assert result[1].role == "assistant"

    def test_tool_result_pairing_in_pipeline(self):
        """Missing tool_result is auto-filled by normalize."""
        h = MessageHistory([
            Message(role="user", content="go"),
            Message(role="assistant", content=[
                ToolUseContent(id="t1", name="bash", input={}),
                ToolUseContent(id="t2", name="read_file", input={}),
            ]),
            Message(role="user", content=[
                ToolResultContent(tool_use_id="t1", content="ok"),
            ]),
            Message(role="assistant", content=[TextContent(text="done")]),
        ])
        result = h.prepare_messages()
        tool_results_msg = result[2]
        blocks = tool_results_msg.content
        ids = {b.tool_use_id for b in blocks if isinstance(b, ToolResultContent)}
        assert "t1" in ids
        assert "t2" in ids

    def test_trailing_thinking_stripped(self):
        h = MessageHistory([
            Message(role="user", content="hi"),
            Message(role="assistant", content=[
                TextContent(text="answer"),
                ThinkingContent(thinking="trailing", signature="sig"),
            ]),
        ])
        result = h.prepare_messages()
        assert len(result[-1].content) == 1
        assert result[-1].content[0].type == "text"

    def test_clean_messages_unchanged(self):
        """Normal messages pass through without modification."""
        h = MessageHistory([
            Message(role="user", content="hello"),
            Message(role="assistant", content=[TextContent(text="hi there")]),
        ])
        result = h.prepare_messages()
        assert len(result) == 2
        assert result[0].content == "hello"
        assert result[1].content[0].text == "hi there"
