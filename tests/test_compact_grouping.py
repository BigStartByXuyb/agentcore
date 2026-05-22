"""Tests for src/compact/grouping.py — message grouping, truncation, and pairing repair."""

from src.compact.grouping import (
    group_messages_by_round,
    truncate_head,
    ensure_tool_result_pairing,
)
from src.types import (
    Message,
    TextContent,
    ToolUseContent,
    ToolResultContent,
)


def _user(text: str) -> Message:
    return Message(role="user", content=text, msg_type="human")


def _assistant(blocks=None, text: str = "ok") -> Message:
    if blocks is None:
        blocks = [TextContent(text=text)]
    return Message(role="assistant", content=blocks, msg_type="assistant")


def _tool_result(tool_use_id: str, content: str = "done") -> Message:
    return Message(
        role="user",
        content=[ToolResultContent(tool_use_id=tool_use_id, content=content)],
        msg_type="tool_result",
    )


def _assistant_with_tool_use(tool_id: str, tool_name: str = "bash") -> Message:
    return Message(
        role="assistant",
        content=[
            TextContent(text="let me run this"),
            ToolUseContent(id=tool_id, name=tool_name, input={"command": "ls"}),
        ],
        msg_type="assistant",
    )


# ---------------------------------------------------------------------------
# group_messages_by_round
# ---------------------------------------------------------------------------

class TestGroupMessagesByRound:
    def test_empty(self):
        assert group_messages_by_round([]) == []

    def test_single_user_message(self):
        msgs = [_user("hello")]
        groups = group_messages_by_round(msgs)
        assert len(groups) == 1
        assert len(groups[0]) == 1

    def test_simple_conversation(self):
        msgs = [_user("hello"), _assistant(text="hi"), _user("bye")]
        groups = group_messages_by_round(msgs)
        assert len(groups) == 2
        assert len(groups[0]) == 1  # [user("hello")]
        assert len(groups[1]) == 2  # [assistant("hi"), user("bye")]

    def test_tool_use_round_trips(self):
        msgs = [
            _user("do something"),
            _assistant_with_tool_use("t1"),
            _tool_result("t1"),
            _assistant_with_tool_use("t2"),
            _tool_result("t2"),
            _assistant(text="all done"),
        ]
        groups = group_messages_by_round(msgs)
        # group 0: [user]
        # group 1: [assistant(t1), tool_result(t1)]
        # group 2: [assistant(t2), tool_result(t2)]
        # group 3: [assistant("all done")]
        assert len(groups) == 4

    def test_multiple_tools_in_one_round(self):
        msgs = [
            _user("check files"),
            _assistant(blocks=[
                TextContent(text="checking"),
                ToolUseContent(id="t1", name="bash", input={}),
                ToolUseContent(id="t2", name="read_file", input={}),
            ]),
            Message(
                role="user",
                content=[
                    ToolResultContent(tool_use_id="t1", content="ok1"),
                    ToolResultContent(tool_use_id="t2", content="ok2"),
                ],
                msg_type="tool_result",
            ),
            _assistant(text="done"),
        ]
        groups = group_messages_by_round(msgs)
        assert len(groups) == 3
        assert len(groups[1]) == 2  # assistant + tool_result


# ---------------------------------------------------------------------------
# truncate_head
# ---------------------------------------------------------------------------

class TestTruncateHead:
    def test_single_group_returns_none(self):
        msgs = [_user("hello"), _assistant(text="hi")]
        # Only 1 group (user + assistant forms group 0, assistant splits group 1)
        # Actually: group 0 = [user], group 1 = [assistant]
        # 2 groups total, so it CAN truncate
        # Let's use just one message
        result = truncate_head([_user("hello")])
        assert result is None

    def test_two_groups_drops_one(self):
        msgs = [
            _user("hello"),
            _assistant(text="hi"),
            _user("bye"),
            _assistant(text="goodbye"),
        ]
        groups = group_messages_by_round(msgs)
        assert len(groups) == 3  # [user], [asst, user], [asst]

        result = truncate_head(msgs, drop_ratio=0.2)
        assert result is not None
        # drops 1 group (max(1, 3*0.2) = 1)
        # remaining: groups[1:] = [assistant("hi"), user("bye")], [assistant("goodbye")]
        # first msg is assistant → synthetic user prepended
        assert result[0].role == "user"
        assert "truncated" in result[0].content.lower()

    def test_preserves_at_least_one_group(self):
        msgs = [_user("a"), _assistant(text="b")]
        groups = group_messages_by_round(msgs)
        # 2 groups: [user], [assistant]
        result = truncate_head(msgs, drop_ratio=0.9)
        assert result is not None
        assert len(result) >= 1

    def test_starts_with_user_no_synthetic(self):
        msgs = [
            _user("first"),
            _assistant(text="a1"),
            _user("second"),
            _assistant(text="a2"),
            _user("third"),
            _assistant(text="a3"),
        ]
        groups = group_messages_by_round(msgs)
        # [user], [asst, user], [asst, user], [asst]
        assert len(groups) == 4

        result = truncate_head(msgs, drop_ratio=0.5)
        assert result is not None
        # drops 2 groups, remaining starts with assistant → synthetic user prepended
        assert result[0].role == "user"

    def test_ensures_pairing_after_truncation(self):
        msgs = [
            _user("start"),
            _assistant_with_tool_use("t1"),
            _tool_result("t1"),
            _assistant(text="middle"),
            _user("next"),
            _assistant_with_tool_use("t2"),
            _tool_result("t2"),
            _assistant(text="end"),
        ]
        result = truncate_head(msgs, drop_ratio=0.3)
        assert result is not None

        # Verify pairing: all tool_use ids have matching tool_results
        use_ids = set()
        result_ids = set()
        for msg in result:
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolUseContent):
                        use_ids.add(block.id)
                    elif isinstance(block, ToolResultContent):
                        result_ids.add(block.tool_use_id)
        assert use_ids <= result_ids  # every use has a result


# ---------------------------------------------------------------------------
# ensure_tool_result_pairing
# ---------------------------------------------------------------------------

class TestEnsureToolResultPairing:
    def test_no_orphans_returns_same(self):
        msgs = [
            _assistant_with_tool_use("t1"),
            _tool_result("t1"),
        ]
        result = ensure_tool_result_pairing(msgs)
        assert result is msgs  # same object (no copy)

    def test_orphaned_tool_use_gets_synthetic_result(self):
        msgs = [
            _assistant_with_tool_use("t1"),
            # no tool_result for t1
        ]
        result = ensure_tool_result_pairing(msgs)
        # Should have the original assistant + a synthetic tool_result message
        assert len(result) == 2
        last_msg = result[-1]
        assert last_msg.role == "user"
        assert isinstance(last_msg.content, list)
        tr = last_msg.content[0]
        assert isinstance(tr, ToolResultContent)
        assert tr.tool_use_id == "t1"
        assert tr.is_error is True

    def test_orphaned_tool_result_gets_stripped(self):
        msgs = [
            _assistant(text="no tool use here"),
            Message(
                role="user",
                content=[
                    ToolResultContent(tool_use_id="orphan", content="stale"),
                    TextContent(text="some text"),
                ],
                msg_type="tool_result",
            ),
        ]
        result = ensure_tool_result_pairing(msgs)
        # orphaned tool_result stripped, but text remains
        user_msg = result[1]
        assert isinstance(user_msg.content, list)
        assert len(user_msg.content) == 1
        assert isinstance(user_msg.content[0], TextContent)

    def test_empty_message_after_stripping_gets_placeholder(self):
        msgs = [
            _assistant(text="nothing"),
            Message(
                role="user",
                content=[ToolResultContent(tool_use_id="orphan", content="stale")],
                msg_type="tool_result",
            ),
        ]
        result = ensure_tool_result_pairing(msgs)
        user_msg = result[1]
        assert isinstance(user_msg.content, list)
        assert len(user_msg.content) == 1
        assert isinstance(user_msg.content[0], TextContent)
        assert "truncated" in user_msg.content[0].text.lower()

    def test_mixed_orphans(self):
        msgs = [
            _assistant_with_tool_use("t1"),
            # missing tool_result for t1
            _assistant(text="text only"),
            Message(
                role="user",
                content=[ToolResultContent(tool_use_id="ghost", content="no use")],
                msg_type="tool_result",
            ),
        ]
        result = ensure_tool_result_pairing(msgs)

        use_ids = set()
        result_ids = set()
        for msg in result:
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolUseContent):
                        use_ids.add(block.id)
                    elif isinstance(block, ToolResultContent):
                        result_ids.add(block.tool_use_id)

        # t1 should now have a synthetic result
        assert "t1" in result_ids
        # ghost should be stripped
        assert "ghost" not in result_ids

    def test_string_content_messages_passed_through(self):
        msgs = [
            _user("hello"),
            _assistant(text="hi"),
        ]
        result = ensure_tool_result_pairing(msgs)
        assert result is msgs
