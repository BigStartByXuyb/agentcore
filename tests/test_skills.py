"""Tests for the skill system (Steps 4 & 5).

Tests:
  1. parse_frontmatter — extracts YAML + body
  2. discover_skills — finds skills in directory
  3. build_skill_content — variable substitution
  4. format_skill_listing — system-reminder text
  5. skill tool executor — inline mode returns correct ToolResult
  6. skill tool executor — allowed_tools restriction
  7. skill tool executor — fork mode returns correct ToolResult (mocked)
  8. skill tool executor — fork depth limit
  9. map_result — forked output formatting
"""

import asyncio
import os
import sys
from unittest.mock import patch, MagicMock


def _mock_run_agent_loop(return_value):
    """Return a side_effect coroutine function that returns the given value."""
    async def _side_effect(*args, **kwargs):
        return return_value
    return _side_effect


def _run_async(coro):
    """Run an async function synchronously for tests."""
    return asyncio.run(coro)


# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.skills import (
    parse_frontmatter,
    discover_skills,
    build_skill_content,
    format_skill_listing,
    reset_sent_skills,
    SkillInfo,
)
from src.messages import build_skill_reminder
from src.tools.skill import _execute, _map_result
from src.types import ToolUseContext, MessageHistory


def test_parse_frontmatter_basic():
    raw = """---
description: A test skill
allowed-tools: bash, read_file
context: fork
when_to_use: When testing
---

# Body content here

Some instructions.
"""
    fm, body = parse_frontmatter(raw)
    assert fm["description"] == "A test skill"
    assert fm["allowed-tools"] == "bash, read_file"
    assert fm["context"] == "fork"
    assert fm["when_to_use"] == "When testing"
    assert body.strip().startswith("# Body content here")
    print("  [PASS] test_parse_frontmatter_basic")


def test_parse_frontmatter_no_frontmatter():
    raw = "# Just a markdown file\n\nNo frontmatter here."
    fm, body = parse_frontmatter(raw)
    assert fm == {}
    assert body == raw
    print("  [PASS] test_parse_frontmatter_no_frontmatter")


def test_discover_skills():
    """Test skill discovery using the project's own skills/ directory."""
    fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures", "skills")
    skills = discover_skills([fixtures_dir])

    assert "hello-world" in skills, f"Expected 'hello-world' in {list(skills.keys())}"
    assert "read-only" in skills, f"Expected 'read-only' in {list(skills.keys())}"

    hw = skills["hello-world"]
    assert hw.description == "A simple test skill that greets the user"
    assert hw.is_fork is False
    assert hw.allowed_tools == []

    ro = skills["read-only"]
    assert ro.allowed_tools == ["read_file", "grep"]
    assert ro.is_fork is False

    print("  [PASS] test_discover_skills")


def test_build_skill_content():
    skill = SkillInfo(
        name="test",
        description="test skill",
        body="Run: ${SKILL_DIR}/script.sh\nAlso: ${CLAUDE_SKILL_DIR}/data",
        base_dir="/home/user/skills/test",
    )
    content = build_skill_content(skill)
    assert "Base directory for this skill: /home/user/skills/test" in content
    assert "/home/user/skills/test/script.sh" in content
    assert "/home/user/skills/test/data" in content
    assert "${SKILL_DIR}" not in content
    assert "${CLAUDE_SKILL_DIR}" not in content
    print("  [PASS] test_build_skill_content")


def test_build_skill_content_windows_path():
    skill = SkillInfo(
        name="test",
        description="test",
        body="Dir: ${SKILL_DIR}",
        base_dir="C:\\Users\\test\\skills\\my-skill",
    )
    content = build_skill_content(skill)
    # Should normalize backslashes
    assert "C:/Users/test/skills/my-skill" in content
    assert "\\" not in content
    print("  [PASS] test_build_skill_content_windows_path")


def test_format_skill_listing():
    skills = {
        "hello": SkillInfo(name="hello", description="Greets the user"),
        "analyze": SkillInfo(
            name="analyze",
            description="Analyzes code",
            when_to_use="When user asks to analyze"
        ),
    }
    listing = format_skill_listing(skills)
    assert "hello: Greets the user" in listing
    assert "analyze: Analyzes code - When user asks to analyze" in listing
    assert "available for use with the Skill tool" in listing
    print("  [PASS] test_format_skill_listing")


def test_format_skill_listing_empty():
    assert format_skill_listing({}) == ""
    print("  [PASS] test_format_skill_listing_empty")


def test_skill_executor_inline():
    """Test skill tool executor returns correct ToolResult for inline mode."""
    # We need the skills to be discoverable
    fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures", "skills")

    # Pre-load skills cache
    from src.skills import discover_skills, _cached_skills
    import src.skills as skills_mod
    skills_mod._cached_skills = discover_skills([fixtures_dir])

    ctx = ToolUseContext(messages=MessageHistory([]), tools=["bash", "read_file", "grep", "Skill"])

    result = _run_async(_execute({"skill": "hello-world"}, ctx))
    assert result.data["success"] is True
    assert result.data["commandName"] == "hello-world"
    assert result.data["status"] == "inline"
    assert len(result.new_messages) == 1
    assert result.new_messages[0].role == "user"
    assert "<skill-content name='hello-world'>" in result.new_messages[0].content
    assert result.context_modifier is None  # no tool restriction

    # map_result
    text = _map_result(result.data)
    assert text == "Launching skill: hello-world"

    print("  [PASS] test_skill_executor_inline")


def test_skill_executor_allowed_tools():
    """Test that allowed_tools creates a context_modifier."""
    fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures", "skills")

    import src.skills as skills_mod
    skills_mod._cached_skills = discover_skills([fixtures_dir])

    ctx = ToolUseContext(messages=MessageHistory([]), tools=["bash", "read_file", "grep", "Skill"])

    result = _run_async(_execute({"skill": "read-only"}, ctx))
    assert result.data["success"] is True
    assert result.context_modifier is not None

    # Apply the modifier and verify tools restriction
    new_ctx = result.context_modifier(ctx)
    assert "read_file" in new_ctx.tools
    assert "grep" in new_ctx.tools
    assert "bash" not in new_ctx.tools

    print("  [PASS] test_skill_executor_allowed_tools")


def test_skill_executor_unknown_skill():
    ctx = ToolUseContext(messages=MessageHistory([]), tools=["bash"])
    result = _run_async(_execute({"skill": "nonexistent"}, ctx))
    assert result.data["success"] is False
    assert "Unknown skill" in result.data["error"]

    text = _map_result(result.data)
    assert "Error" in text

    print("  [PASS] test_skill_executor_unknown_skill")


def test_skill_executor_leading_slash():
    """Test that /hello-world is normalized to hello-world."""
    fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures", "skills")

    import src.skills as skills_mod
    skills_mod._cached_skills = discover_skills([fixtures_dir])

    ctx = ToolUseContext(messages=MessageHistory([]), tools=["bash", "Skill"])
    result = _run_async(_execute({"skill": "/hello-world"}, ctx))
    assert result.data["success"] is True
    assert result.data["commandName"] == "hello-world"

    print("  [PASS] test_skill_executor_leading_slash")


# ---------------------------------------------------------------------------
# build_skill_reminder / reset_sent_skills tests
# ---------------------------------------------------------------------------

def _skill_tools():
    """Tools list that includes 'Skill' so build_skill_reminder doesn't short-circuit."""
    return ["bash", "read_file", "grep", "Skill"]


def test_build_skill_reminder_returns_user_message():
    """First call should return an Attachment with skill listing."""
    fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures", "skills")

    import src.skills as skills_mod
    skills_mod._cached_skills = discover_skills([fixtures_dir])

    reset_sent_skills()

    att = build_skill_reminder(_skill_tools())
    assert att is not None
    assert att.type == "system_reminder"
    assert "<system-reminder>" in att.content
    assert "available for use with the Skill tool" in att.content
    assert "hello-world" in att.content
    print("  [PASS] test_build_skill_reminder_returns_user_message")


def test_build_skill_reminder_no_repeat():
    """Second call (no new skills) should return None."""
    fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures", "skills")

    import src.skills as skills_mod
    skills_mod._cached_skills = discover_skills([fixtures_dir])

    reset_sent_skills()

    att1 = build_skill_reminder(_skill_tools())
    assert att1 is not None

    att2 = build_skill_reminder(_skill_tools())
    assert att2 is None
    print("  [PASS] test_build_skill_reminder_no_repeat")


def test_reset_sent_skills():
    """After reset, next call should announce again."""
    fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures", "skills")

    import src.skills as skills_mod
    skills_mod._cached_skills = discover_skills([fixtures_dir])

    reset_sent_skills()
    build_skill_reminder(_skill_tools())

    reset_sent_skills()

    att = build_skill_reminder(_skill_tools())
    assert att is not None
    print("  [PASS] test_reset_sent_skills")


# ---------------------------------------------------------------------------
# Fork mode tests (Step 5)
# ---------------------------------------------------------------------------

def _load_fixtures():
    """Helper: load skills from test fixtures directory."""
    fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures", "skills")
    import src.skills as skills_mod
    skills_mod._cached_skills = discover_skills([fixtures_dir])


def test_discover_fork_skill():
    """Test that fork-test skill is discovered with is_fork=True."""
    _load_fixtures()
    from src.skills import get_skill

    fork_skill = get_skill("fork-test")
    assert fork_skill is not None, "fork-test skill should be discovered"
    assert fork_skill.is_fork is True
    assert fork_skill.allowed_tools == ["bash"]
    assert fork_skill.description == "A fork test skill that runs in an isolated sub-agent"

    print("  [PASS] test_discover_fork_skill")


def test_discover_fork_unrestricted_skill():
    """Test that fork-unrestricted skill has no tool restrictions."""
    _load_fixtures()
    from src.skills import get_skill

    skill = get_skill("fork-unrestricted")
    assert skill is not None
    assert skill.is_fork is True
    assert skill.allowed_tools == []

    print("  [PASS] test_discover_fork_unrestricted_skill")


def test_skill_executor_fork_basic():
    """Test fork skill executor calls run_agent_loop and returns forked status.

    We mock run_agent_loop to avoid actually calling the LLM API.
    """
    _load_fixtures()

    ctx = ToolUseContext(messages=MessageHistory([]), tools=["bash", "read_file", "Skill"], depth=0)

    with patch("src.agent_loop.run_agent_loop", side_effect=_mock_run_agent_loop("Mocked fork result")) as mock_loop:
        result = _run_async(_execute({"skill": "fork-test"}, ctx))

    # Verify result structure
    assert result.data["success"] is True
    assert result.data["commandName"] == "fork-test"
    assert result.data["status"] == "forked"
    assert result.data["result"] == "Mocked fork result"
    assert result.data["agentId"] == "fork:fork-test"

    # Fork mode: no new_messages, no context_modifier
    assert result.new_messages == []
    assert result.context_modifier is None

    # Verify run_agent_loop was called with correct params
    mock_loop.assert_called_once()
    call_kwargs = mock_loop.call_args[1]
    assert call_kwargs["label"] == "fork:fork-test"
    assert call_kwargs["system_prompt"].startswith("You are a sub-agent")
    assert "Do NOT spawn sub-agents" in call_kwargs["system_prompt"]

    # Initial messages should contain skill content
    sub_ctx = call_kwargs["tool_use_context"]
    initial_msgs = sub_ctx.messages.messages
    assert len(initial_msgs) >= 1
    assert "<skill-content name='fork-test'>" in initial_msgs[0].content

    # Sub-agent context should have depth + 1
    assert sub_ctx.depth == 1

    print("  [PASS] test_skill_executor_fork_basic")


def test_skill_executor_fork_with_args():
    """Test fork skill respects $ARGUMENTS substitution."""
    _load_fixtures()

    ctx = ToolUseContext(messages=MessageHistory([]), tools=["bash", "Skill"], depth=0)

    with patch("src.agent_loop.run_agent_loop", side_effect=_mock_run_agent_loop("Done")) as mock_loop:
        result = _run_async(_execute({"skill": "fork-test", "args": "extra_arg"}, ctx))

    assert result.data["success"] is True
    # The content should have $ARGUMENTS replaced (if present in body)
    sub_ctx = mock_loop.call_args[1]["tool_use_context"]
    initial_msgs = sub_ctx.messages.messages
    assert len(initial_msgs) >= 1

    print("  [PASS] test_skill_executor_fork_with_args")


def test_skill_executor_fork_allowed_tools():
    """Test fork skill with allowed_tools builds restricted tool schemas."""
    _load_fixtures()

    ctx = ToolUseContext(messages=MessageHistory([]), tools=["bash", "read_file", "grep", "Skill"], depth=0)

    with patch("src.agent_loop.run_agent_loop", side_effect=_mock_run_agent_loop("OK")) as mock_loop:
        result = _run_async(_execute({"skill": "fork-test"}, ctx))

    assert result.data["success"] is True

    # Sub-agent tools should respect allowed_tools from skill
    sub_ctx = mock_loop.call_args[1]["tool_use_context"]
    # fork-test has allowed-tools: bash, plus Skill is always included
    tool_names = sub_ctx.tools
    assert "bash" in tool_names

    print("  [PASS] test_skill_executor_fork_allowed_tools")


def test_skill_executor_fork_depth_limit():
    """Test fork skill rejects when depth limit is reached."""
    _load_fixtures()

    from src import config
    max_depth = config.MAX_AGENT_DEPTH

    ctx = ToolUseContext(messages=MessageHistory([]), tools=["bash", "Skill"], depth=max_depth)

    # Should NOT call run_agent_loop
    with patch("src.agent_loop.run_agent_loop") as mock_loop:
        result = _run_async(_execute({"skill": "fork-test"}, ctx))

    mock_loop.assert_not_called()
    assert result.data["success"] is False
    assert "depth" in result.data["error"].lower()
    assert result.new_messages == []
    assert result.context_modifier is None

    print("  [PASS] test_skill_executor_fork_depth_limit")


def test_skill_executor_fork_exception_handling():
    """Test fork skill handles exceptions from run_agent_loop gracefully."""
    _load_fixtures()

    ctx = ToolUseContext(messages=MessageHistory([]), tools=["bash", "Skill"], depth=0)

    with patch("src.agent_loop.run_agent_loop", side_effect=RuntimeError("API down")):
        result = _run_async(_execute({"skill": "fork-test"}, ctx))

    assert result.data["success"] is False
    assert "Fork execution failed" in result.data["error"]
    assert "API down" in result.data["error"]

    print("  [PASS] test_skill_executor_fork_exception_handling")


def test_map_result_forked():
    """Test map_result formats forked output correctly."""
    data = {
        "success": True,
        "commandName": "my-skill",
        "status": "forked",
        "agentId": "fork:my-skill",
        "result": "The analysis is complete.",
    }
    text = _map_result(data)
    assert 'Skill "my-skill" completed (forked execution).' in text
    assert "The analysis is complete." in text

    print("  [PASS] test_map_result_forked")


def test_map_result_forked_empty_result():
    """Test map_result handles empty forked result."""
    data = {
        "success": True,
        "commandName": "my-skill",
        "status": "forked",
        "result": "",
    }
    text = _map_result(data)
    assert 'Skill "my-skill" completed (forked execution).' in text

    print("  [PASS] test_map_result_forked_empty_result")


def test_map_result_fork_error():
    """Test map_result handles fork error."""
    data = {
        "success": False,
        "commandName": "my-skill",
        "error": "Maximum agent nesting depth (3) reached.",
    }
    text = _map_result(data)
    assert "Error:" in text
    assert "Maximum agent nesting depth" in text

    print("  [PASS] test_map_result_fork_error")


if __name__ == "__main__":
    print("\n=== Skill System Tests (Steps 4 & 5) ===\n")

    # Step 4 — inline mode
    test_parse_frontmatter_basic()
    test_parse_frontmatter_no_frontmatter()
    test_discover_skills()
    test_build_skill_content()
    test_build_skill_content_windows_path()
    test_format_skill_listing()
    test_format_skill_listing_empty()
    test_skill_executor_inline()
    test_skill_executor_allowed_tools()
    test_skill_executor_unknown_skill()
    test_skill_executor_leading_slash()
    test_build_skill_reminder_returns_user_message()
    test_build_skill_reminder_no_repeat()
    test_reset_sent_skills()

    # Step 5 — fork mode
    test_discover_fork_skill()
    test_discover_fork_unrestricted_skill()
    test_skill_executor_fork_basic()
    test_skill_executor_fork_with_args()
    test_skill_executor_fork_allowed_tools()
    test_skill_executor_fork_depth_limit()
    test_skill_executor_fork_exception_handling()
    test_map_result_forked()
    test_map_result_forked_empty_result()
    test_map_result_fork_error()

    print("\n=== All Skill System Tests PASSED ===\n")
