"""Tests for PermissionEngine — internal editable paths (plan file auto-allow)."""

import os

import pytest

from src.permissions import PermissionEngine, PermissionRule, is_internal_editable_path


# ---------------------------------------------------------------------------
# is_internal_editable_path
# ---------------------------------------------------------------------------

class TestIsInternalEditablePath:
    def test_plan_file_in_plans_dir(self):
        plans_dir = os.path.join(os.path.expanduser("~"), ".my-agent", "plans")
        path = os.path.join(plans_dir, "plan-abc123.md")
        assert is_internal_editable_path(path) is True

    def test_non_plan_file_rejected(self):
        assert is_internal_editable_path("/some/other/file.py") is False

    def test_memory_file_in_memory_dir(self):
        mem_dir = os.path.join(os.path.expanduser("~"), ".my-agent", "memory")
        path = os.path.join(mem_dir, "user_role.md")
        assert is_internal_editable_path(path) is True

    def test_nested_memory_file(self):
        mem_dir = os.path.join(os.path.expanduser("~"), ".my-agent", "memory")
        path = os.path.join(mem_dir, "subdir", "note.md")
        assert is_internal_editable_path(path) is True

    def test_file_outside_my_agent_rejected(self):
        path = os.path.join(os.path.expanduser("~"), "Documents", "notes.md")
        assert is_internal_editable_path(path) is False

    def test_my_agent_root_file_rejected(self):
        """Files directly in ~/.my-agent/ (not in plans/ or memory/) are not auto-allowed."""
        path = os.path.join(os.path.expanduser("~"), ".my-agent", "config.json")
        assert is_internal_editable_path(path) is False


# ---------------------------------------------------------------------------
# PermissionEngine.check — internal path auto-allow
# ---------------------------------------------------------------------------

class TestPermissionEngineInternalPaths:
    def test_plan_file_auto_allowed(self):
        engine = PermissionEngine()
        plans_dir = os.path.join(os.path.expanduser("~"), ".my-agent", "plans")
        plan_path = os.path.join(plans_dir, "plan-abc123.md")
        decision = engine.check("write_file", plan_path)
        assert decision.behavior == "allow"

    def test_memory_file_auto_allowed(self):
        engine = PermissionEngine()
        mem_dir = os.path.join(os.path.expanduser("~"), ".my-agent", "memory")
        mem_path = os.path.join(mem_dir, "feedback_testing.md")
        decision = engine.check("write_file", mem_path)
        assert decision.behavior == "allow"

    def test_regular_file_still_asks(self):
        engine = PermissionEngine()
        decision = engine.check("write_file", "/some/project/file.py")
        assert decision.behavior == "ask"

    def test_deny_rule_overrides_internal_path(self):
        """Explicit deny rules take precedence over internal path auto-allow."""
        engine = PermissionEngine()
        plans_dir = os.path.join(os.path.expanduser("~"), ".my-agent", "plans")
        plan_path = os.path.join(plans_dir, "plan-abc123.md")
        engine.add_session_rule(PermissionRule(
            tool_name="write_file",
            content_pattern=plan_path,
            behavior="deny",
            source="session",
        ))
        decision = engine.check("write_file", plan_path)
        assert decision.behavior == "deny"

    def test_edit_file_plan_auto_allowed(self):
        engine = PermissionEngine()
        plans_dir = os.path.join(os.path.expanduser("~"), ".my-agent", "plans")
        plan_path = os.path.join(plans_dir, "plan-xyz.md")
        decision = engine.check("edit_file", plan_path)
        assert decision.behavior == "allow"

    def test_bash_not_affected_by_internal_path(self):
        """Internal path check only applies to file write tools, not bash."""
        engine = PermissionEngine()
        plans_dir = os.path.join(os.path.expanduser("~"), ".my-agent", "plans")
        plan_path = os.path.join(plans_dir, "plan-abc123.md")
        decision = engine.check("bash", plan_path)
        assert decision.behavior == "ask"
