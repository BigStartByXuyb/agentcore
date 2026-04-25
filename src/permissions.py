"""Permission engine — rule-based tool access control.

Corresponds to Claude Code's permission system:
  - permissionRuleParser.ts  → parse_rule() / match_content()
  - permissions.ts           → PermissionEngine.check()
  - useCanUseTool.tsx        → PermissionRequest event (async Future)

Three rule sources with descending priority:
  session  — runtime, memory-only, lost on restart
  project  — agent-permissions.json in project root
  user     — ~/.my-agent/permissions.json global defaults

Check flow: deny-first scan, then allow scan, then default ask.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

PermissionBehavior = Literal["allow", "deny", "ask"]
RuleSource = Literal["session", "project", "user", "default"]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PermissionRule:
    tool_name: str
    content_pattern: str | None
    behavior: PermissionBehavior
    source: RuleSource

    def __repr__(self) -> str:
        pat = f"({self.content_pattern})" if self.content_pattern else ""
        return f"{self.behavior}:{self.tool_name}{pat}[{self.source}]"


@dataclass
class PermissionDecision:
    behavior: PermissionBehavior
    matched_rule: PermissionRule | None = None
    message: str | None = None


# ---------------------------------------------------------------------------
# Rule parsing
# ---------------------------------------------------------------------------

def parse_rule(rule_str: str) -> tuple[str, str | None]:
    """Parse 'Bash(npm install*)' → ('Bash', 'npm install*').
       Parse 'ReadFile' → ('ReadFile', None).
    """
    m = re.match(r'^([A-Za-z_][\w]*)(?:\((.+)\))?$', rule_str.strip())
    if not m:
        raise ValueError(f"Invalid permission rule: {rule_str!r}")
    tool_name = m.group(1)
    content = m.group(2)
    return tool_name, content


def match_content(pattern: str | None, content: str | None) -> bool:
    """Match content against a pattern.

    Supports:
      - None pattern  → matches everything
      - '*'           → matches everything
      - 'prefix*'     → prefix match
      - exact string  → exact match
    """
    if pattern is None:
        return True
    if content is None:
        return pattern == "*"
    if pattern == "*":
        return True
    if pattern.endswith("*") and not pattern.endswith("\\*"):
        prefix = pattern[:-1]
        return content.startswith(prefix)
    return content == pattern


# ---------------------------------------------------------------------------
# Default rules — built-in safe defaults
# ---------------------------------------------------------------------------

_DEFAULT_RULES: list[PermissionRule] = [
    PermissionRule(tool_name="read_file", content_pattern=None, behavior="allow", source="default"),
    PermissionRule(tool_name="grep", content_pattern=None, behavior="allow", source="default"),
    PermissionRule(tool_name="Skill", content_pattern=None, behavior="allow", source="default"),
    PermissionRule(tool_name="Agent", content_pattern=None, behavior="allow", source="default"),
]

# ---------------------------------------------------------------------------
# Permission engine
# ---------------------------------------------------------------------------

class PermissionEngine:
    """Global permission validator — shared by all tools.

    Rule priority: session > project > user > default.
    Check order: deny-first, then allow, then default ask.
    """

    def __init__(
        self,
        *,
        user_config: str | None = None,
        project_config: str | None = None,
        silent: bool = False,
    ) -> None:
        self._user_config = user_config
        self._project_config = project_config
        self._user_rules: list[PermissionRule] = []
        self._project_rules: list[PermissionRule] = []
        self._session_rules: list[PermissionRule] = []
        self._silent = silent

        if user_config:
            self._user_rules = self._load_config(user_config, "user")
        if project_config:
            self._project_rules = self._load_config(project_config, "project")

    def reload(self) -> None:
        """Reload user and project rules from disk. Session rules are preserved."""
        self._user_rules = (
            self._load_config(self._user_config, "user")
            if self._user_config else []
        )
        self._project_rules = (
            self._load_config(self._project_config, "project")
            if self._project_config else []
        )

    def _load_config(self, path: str, source: RuleSource) -> list[PermissionRule]:
        path = os.path.expanduser(path)
        if not os.path.isfile(path):
            logger.debug("Permission config not found: %s", path)
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to load permission config %s: %s", path, e)
            return []

        rules: list[PermissionRule] = []
        for behavior in ("allow", "deny"):
            for rule_str in data.get(behavior, []):
                try:
                    tool_name, content_pattern = parse_rule(rule_str)
                    rules.append(PermissionRule(
                        tool_name=tool_name,
                        content_pattern=content_pattern,
                        behavior=behavior,
                        source=source,
                    ))
                except ValueError as e:
                    logger.warning("Skipping invalid rule in %s: %s", path, e)
        return rules

    def add_session_rule(self, rule: PermissionRule) -> None:
        rule.source = "session"
        self._session_rules.append(rule)

    def _all_rules(self) -> list[PermissionRule]:
        return self._session_rules + self._project_rules + self._user_rules + _DEFAULT_RULES

    def check(self, tool_name: str, content: str | None = None) -> PermissionDecision:
        """Check permission for a tool invocation.

        1. Scan all deny rules (highest priority first) — hit → deny
        2. Scan all allow rules (highest priority first) — hit → allow
        3. No match → ask
        """
        rules = self._all_rules()

        for rule in rules:
            if rule.behavior != "deny":
                continue
            if self._matches(rule, tool_name, content):
                return PermissionDecision(
                    behavior="deny",
                    matched_rule=rule,
                    message=f"Denied by rule: {rule}",
                )

        for rule in rules:
            if rule.behavior != "allow":
                continue
            if self._matches(rule, tool_name, content):
                return PermissionDecision(behavior="allow", matched_rule=rule)

        return PermissionDecision(
            behavior="deny" if self._silent else "ask",
            message="Sub-agent cannot prompt for permission" if self._silent else None,
        )

    def as_silent(self) -> "PermissionEngine":
        """Create a silent copy for sub-agents — ask becomes deny."""
        clone = PermissionEngine(silent=True)
        clone._user_rules = self._user_rules
        clone._project_rules = self._project_rules
        clone._session_rules = self._session_rules
        return clone

    @staticmethod
    def _matches(rule: PermissionRule, tool_name: str, content: str | None) -> bool:
        if rule.tool_name.lower() != tool_name.lower() and rule.tool_name.lower() != "*":
            return False
        return match_content(rule.content_pattern, content)
