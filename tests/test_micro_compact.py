"""Tests for Layer 1: Micro Compact — tool_result clearing by round."""

import time
from unittest import mock

from src.compact.micro_compact import (
    micro_compact,
    should_micro_compact,
    CLEARED_MESSAGE,
    _collect_rounds,
    _find_last_assistant,
)
from src.core.types import (
    Message, ToolUseContent, ToolResultContent, TextContent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assistant_with_tools(*tool_calls: tuple[str, str]) -> Message:
    """Create an assistant message with tool_use blocks.

    Each tool_call is (tool_id, tool_name).
    """
    content = [ToolUseContent(id=tid, name=tname, input={}) for tid, tname in tool_calls]
    return Message(role="assistant", content=content, msg_type="assistant")


def _tool_result(*tool_ids: str, text: str = "some output") -> Message:
    """Create a user message with tool_result blocks."""
    content = [ToolResultContent(tool_use_id=tid, content=text) for tid in tool_ids]
    return Message(role="user", content=content, msg_type="tool_result")


def _user(text: str = "hello") -> Message:
    return Message(role="user", content=text, msg_type="human")


def _assistant_text(text: str = "ok") -> Message:
    return Message(role="assistant", content=[TextContent(text=text)], msg_type="assistant")


# ---------------------------------------------------------------------------
# _collect_rounds
# ---------------------------------------------------------------------------

class TestCollectRounds:
    def test_empty(self):
        assert _collect_rounds([]) == []

    def test_no_tool_use(self):
        msgs = [_user(), _assistant_text()]
        assert _collect_rounds(msgs) == []

    def test_single_round_single_tool(self):
        msgs = [_assistant_with_tools(("t1", "bash"))]
        rounds = _collect_rounds(msgs)
        assert rounds == [["t1"]]

    def test_single_round_multiple_tools(self):
        msgs = [_assistant_with_tools(("t1", "bash"), ("t2", "read_file"), ("t3", "grep"))]
        rounds = _collect_rounds(msgs)
        assert rounds == [["t1", "t2", "t3"]]

    def test_multiple_rounds(self):
        msgs = [
            _assistant_with_tools(("t1", "bash")),
            _tool_result("t1"),
            _assistant_with_tools(("t2", "read_file")),
            _tool_result("t2"),
            _assistant_with_tools(("t3", "grep")),
            _tool_result("t3"),
        ]
        rounds = _collect_rounds(msgs)
        assert rounds == [["t1"], ["t2"], ["t3"]]

    def test_all_tool_types_included(self):
        """All tools are tracked, not just specific ones."""
        msgs = [
            _assistant_with_tools(("t1", "bash"), ("t2", "skill"), ("t3", "agent")),
        ]
        rounds = _collect_rounds(msgs)
        assert rounds == [["t1", "t2", "t3"]]


# ---------------------------------------------------------------------------
# micro_compact
# ---------------------------------------------------------------------------

class TestMicroCompact:
    def test_basic_clearing(self):
        """8 rounds, keep_recent=3, first 5 rounds cleared."""
        msgs = []
        for i in range(8):
            msgs.append(_assistant_with_tools((f"t{i}", "bash")))
            msgs.append(_tool_result(f"t{i}", text=f"output {i}"))

        cleared = micro_compact(msgs, keep_recent=3)

        assert cleared == 5
        for msg in msgs:
            if msg.msg_type != "tool_result":
                continue
            for block in msg.content:
                if block.tool_use_id in {"t5", "t6", "t7"}:
                    assert block.content != CLEARED_MESSAGE
                else:
                    assert block.content == CLEARED_MESSAGE

    def test_round_grouping_all_or_nothing(self):
        """Multi-tool round: entire round kept or entirely cleared."""
        msgs = [
            _assistant_with_tools(("t1", "bash"), ("t2", "read_file")),
            _tool_result("t1", "t2", text="old output"),
            _assistant_with_tools(("t3", "grep")),
            _tool_result("t3", text="new output"),
        ]

        cleared = micro_compact(msgs, keep_recent=1)

        assert cleared == 2
        for block in msgs[1].content:
            assert block.content == CLEARED_MESSAGE
        assert msgs[3].content[0].content == "new output"

    def test_all_tools_cleared(self):
        """All tool types are subject to clearing, not just bash/read_file/grep."""
        msgs = [
            _assistant_with_tools(("t1", "skill")),
            _tool_result("t1", text="skill output"),
            _assistant_with_tools(("t2", "agent")),
            _tool_result("t2", text="agent output"),
            _assistant_with_tools(("t3", "bash")),
            _tool_result("t3", text="bash output"),
        ]

        cleared = micro_compact(msgs, keep_recent=1)

        assert cleared == 2
        assert msgs[1].content[0].content == CLEARED_MESSAGE
        assert msgs[3].content[0].content == CLEARED_MESSAGE
        assert msgs[5].content[0].content == "bash output"

    def test_already_cleared_not_recounted(self):
        """Already cleared results should not increment the counter."""
        msgs = [
            _assistant_with_tools(("t1", "bash")),
            _tool_result("t1", text=CLEARED_MESSAGE),
            _assistant_with_tools(("t2", "bash")),
            _tool_result("t2", text="output"),
        ]

        cleared = micro_compact(msgs, keep_recent=1)
        assert cleared == 0

    def test_keep_recent_greater_than_total(self):
        """No clearing when keep_recent >= total rounds."""
        msgs = [
            _assistant_with_tools(("t1", "bash")),
            _tool_result("t1", text="output"),
        ]

        cleared = micro_compact(msgs, keep_recent=5)
        assert cleared == 0
        assert msgs[1].content[0].content == "output"

    def test_keep_recent_minimum_is_1(self):
        """keep_recent=0 should be clamped to 1."""
        msgs = [
            _assistant_with_tools(("t1", "bash")),
            _tool_result("t1", text="output"),
        ]

        cleared = micro_compact(msgs, keep_recent=0)
        assert cleared == 0

    def test_preserves_is_error(self):
        """Cleared results should preserve the is_error flag."""
        content = [ToolResultContent(tool_use_id="t1", content="error output", is_error=True)]
        msgs = [
            _assistant_with_tools(("t1", "bash")),
            Message(role="user", content=content, msg_type="tool_result"),
            _assistant_with_tools(("t2", "bash")),
            _tool_result("t2", text="ok"),
        ]

        cleared = micro_compact(msgs, keep_recent=1)
        assert cleared == 1
        assert msgs[1].content[0].content == CLEARED_MESSAGE
        assert msgs[1].content[0].is_error is True

    def test_empty_messages(self):
        cleared = micro_compact([], keep_recent=3)
        assert cleared == 0

    def test_idempotent(self):
        """Running micro_compact twice should not clear anything extra."""
        msgs = [
            _assistant_with_tools(("t1", "bash")),
            _tool_result("t1", text="output 1"),
            _assistant_with_tools(("t2", "bash")),
            _tool_result("t2", text="output 2"),
        ]

        first_cleared = micro_compact(msgs, keep_recent=1)
        assert first_cleared == 1

        second_cleared = micro_compact(msgs, keep_recent=1)
        assert second_cleared == 0


# ---------------------------------------------------------------------------
# should_micro_compact
# ---------------------------------------------------------------------------

def _mock_cfg(**kwargs):
    """Return a mock config object with the given attributes."""
    cfg = mock.MagicMock()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


class TestShouldMicroCompact:
    def test_disabled(self):
        with mock.patch("src.compact.micro_compact.config") as m:
            m.get.return_value = _mock_cfg(micro_compact_enabled=False)
            assert should_micro_compact([]) is False

    def test_no_cache_always_true(self):
        msgs = [_assistant_text()]
        with mock.patch("src.compact.micro_compact.config") as m:
            m.get.return_value = _mock_cfg(
                micro_compact_enabled=True,
                prompt_cache_enabled=False,
            )
            assert should_micro_compact(msgs) is True

    def test_cache_hot_no_compact(self):
        """Cache is warm (recent assistant message) — don't compact."""
        msgs = [Message(role="assistant", content=[TextContent(text="hi")],
                        msg_type="assistant", timestamp=time.time())]
        with mock.patch("src.compact.micro_compact.config") as m:
            m.get.return_value = _mock_cfg(
                micro_compact_enabled=True,
                prompt_cache_enabled=True,
                prompt_cache_ttl_minutes=5,
            )
            assert should_micro_compact(msgs) is False

    def test_cache_expired_do_compact(self):
        """Cache has expired (old assistant message) — compact."""
        old_time = time.time() - 600  # 10 minutes ago
        msgs = [Message(role="assistant", content=[TextContent(text="hi")],
                        msg_type="assistant", timestamp=old_time)]
        with mock.patch("src.compact.micro_compact.config") as m:
            m.get.return_value = _mock_cfg(
                micro_compact_enabled=True,
                prompt_cache_enabled=True,
                prompt_cache_ttl_minutes=5,
            )
            assert should_micro_compact(msgs) is True

    def test_no_assistant_message(self):
        """No assistant messages — nothing to base timing on, skip."""
        msgs = [_user()]
        with mock.patch("src.compact.micro_compact.config") as m:
            m.get.return_value = _mock_cfg(
                micro_compact_enabled=True,
                prompt_cache_enabled=True,
                prompt_cache_ttl_minutes=5,
            )
            assert should_micro_compact(msgs) is False


# ---------------------------------------------------------------------------
# _find_last_assistant
# ---------------------------------------------------------------------------

class TestFindLastAssistant:
    def test_found(self):
        msgs = [_user(), _assistant_text("first"), _user(), _assistant_text("second")]
        result = _find_last_assistant(msgs)
        assert result is not None
        assert result.content[0].text == "second"

    def test_not_found(self):
        msgs = [_user()]
        assert _find_last_assistant(msgs) is None

    def test_empty(self):
        assert _find_last_assistant([]) is None
