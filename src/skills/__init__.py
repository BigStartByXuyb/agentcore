"""Skill discovery, loading and content building.

Corresponds to Claude Code's:
  - src/skills/loadSkillsDir.ts   → loadSkillsFromSkillsDir(), parseFrontmatter
  - src/tools/SkillTool/prompt.ts → formatCommandsWithinBudget(), skill listing
  - src/tools/SkillTool/SkillTool.ts → inline execution (call() path)

Design overview:
  - discover_skills()   → scan skills/ dirs, parse each SKILL.md frontmatter
  - build_skill_content() → strip frontmatter, replace ${SKILL_DIR}, prepend base dir header
  - format_skill_listing() → build the <system-reminder> text for LLM awareness

Skills are NOT registered in ALL_TOOLS directly. Instead the SkillTool
(src/tools/skill.py) is the single tool — it receives a skill name,
looks it up via get_skill(), builds content, and returns the appropriate
ToolResult with new_messages / context_modifier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from src.frontmatter import parse_frontmatter  # noqa: F401 — re-exported for back-compat
from src.core.types import Attachment


# ---------------------------------------------------------------------------
# SkillInfo — parsed representation of one skill
# ---------------------------------------------------------------------------

@dataclass
class SkillInfo:
    """Parsed skill metadata + content from a SKILL.md file.

    Corresponds to Claude Code's PromptCommand object built by
    createSkillCommand() in loadSkillsDir.ts.

    Fields:
      name:         directory name (= skill name). E.g. "project-analyzer"
      description:  from frontmatter 'description' or first paragraph
      is_fork:      True → run as sub-agent (Step 5); False → inline (Step 4)
      allowed_tools: whitelist of tools this skill may use (empty = no restriction)
      body:         SKILL.md content with frontmatter stripped
      base_dir:     absolute path to the skill directory (for ${SKILL_DIR} replacement)
      when_to_use:  optional hint for LLM about when to invoke this skill
    """

    name: str
    description: str
    is_fork: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    body: str = ""
    base_dir: str = ""
    when_to_use: str = ""


# ---------------------------------------------------------------------------
# Frontmatter parsing — now in src/frontmatter.py
# parse_frontmatter is re-exported above for back-compat
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Skill content builder
# ---------------------------------------------------------------------------

def build_skill_content(skill: SkillInfo) -> str:
    """Build the final skill content that gets injected into conversation.

    Mirrors Claude Code's getPromptForCommand() in loadSkillsDir.ts:
      1. Prepend "Base directory for this skill: <path>" header
      2. Replace ${SKILL_DIR} with the skill's directory path
      3. Replace ${CLAUDE_SKILL_DIR} with the same (alias)

    The result is the full text that goes into the <skill-content> user message.
    """
    content = skill.body

    if skill.base_dir:
        # Normalize backslashes on Windows for shell compatibility
        norm_dir = skill.base_dir.replace("\\", "/")
        content = f"Base directory for this skill: {norm_dir}\n\n{content}"
        # Replace both variable names (CC uses CLAUDE_SKILL_DIR, we also support SKILL_DIR)
        content = content.replace("${SKILL_DIR}", norm_dir)
        content = content.replace("${CLAUDE_SKILL_DIR}", norm_dir)

    return content


# ---------------------------------------------------------------------------
# Skill discovery
# ---------------------------------------------------------------------------

def _load_skill_from_dir(skill_dir: str) -> SkillInfo | None:
    """Load a single skill from its directory (skill_dir/SKILL.md).

    Mirrors loadSkillsFromSkillsDir() per-entry logic in loadSkillsDir.ts.
    Returns None if SKILL.md is missing or unparseable.
    """
    skill_file = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_file):
        return None

    try:
        with open(skill_file, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None

    fm, body = parse_frontmatter(raw)
    name = os.path.basename(skill_dir)

    # --- Parse frontmatter fields ---
    # description: string or auto-extract from first paragraph
    description = fm.get("description", "")
    if not description:
        description = _extract_description(body, name)

    # allowed-tools: string (comma-sep) or list
    allowed_tools = _parse_allowed_tools(fm.get("allowed-tools"))

    # context: 'fork' or 'inline' (default)
    is_fork = fm.get("context") == "fork"

    # when_to_use: optional LLM hint
    when_to_use = str(fm.get("when_to_use", "") or "")

    return SkillInfo(
        name=name,
        description=str(description),
        is_fork=is_fork,
        allowed_tools=allowed_tools,
        body=body,
        base_dir=os.path.abspath(skill_dir),
        when_to_use=when_to_use,
    )


def _extract_description(body: str, fallback_name: str) -> str:
    """Extract a short description from the first non-empty line of the body.

    Mirrors extractDescriptionFromMarkdown() in markdownConfigLoader.ts.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            # Remove markdown formatting
            stripped = stripped.lstrip("*_`> ")
            return stripped[:200]
    return f"Skill: {fallback_name}"


def _parse_allowed_tools(value: Any) -> list[str]:
    """Parse the allowed-tools frontmatter field.

    Accepts:
      - string: comma-separated tool names  ("bash, read_file")
      - list:   ["bash", "read_file"]
      - None:   returns []
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return []


def discover_skills(skills_dirs: list[str] | None = None) -> dict[str, SkillInfo]:
    """Scan skills directories and return {name: SkillInfo}.

    Mirrors getSkillDirCommands() in loadSkillsDir.ts.

    Default search paths (if skills_dirs is None):
      1. <project_root>/skills/          (project-level)
      2. <project_root>/.claude/skills/  (project .claude dir)

    Each directory is expected to contain subdirectories, each with a SKILL.md:
      skills/
        my-skill/
          SKILL.md
        another-skill/
          SKILL.md

    Later entries override earlier ones (project skills override global).
    """
    if skills_dirs is None:
        from src.core.config import get_skill_dirs
        skills_dirs = get_skill_dirs()

    skills: dict[str, SkillInfo] = {}

    for base_dir in skills_dirs:
        if not os.path.isdir(base_dir):
            continue

        try:
            entries = os.listdir(base_dir)
        except OSError:
            continue

        for entry in sorted(entries):
            entry_path = os.path.join(base_dir, entry)
            if not os.path.isdir(entry_path):
                continue

            skill = _load_skill_from_dir(entry_path)
            if skill is not None:
                skills[skill.name] = skill

    return skills


# ---------------------------------------------------------------------------
# Skill listing for system-reminder injection
# ---------------------------------------------------------------------------

_MAX_LISTING_DESC_CHARS = 250


def format_skill_listing(
    skills: dict[str, SkillInfo],
    *,
    exclude_fork: bool = False,
    filter_names: list[str] | None = None,
) -> str:
    """Build the system-reminder text that tells the LLM which skills are available.

    Mirrors formatCommandsWithinBudget() in prompt.ts.
    Output format example:

      The following skills are available for use with the Skill tool:

      - my-skill: Does something useful
      - another-skill: Analyzes code - Use when user asks to analyze

    The LLM reads this listing and decides when to invoke the Skill tool.

    Parameters:
        exclude_fork:  If True, skip fork-mode skills from the listing.
                       Used by sub-agents which may only use inline skills
                       (fork skills would create nested agent loops).
        filter_names:  If not None, only include skills whose name is in
                       this list.  Implements per-agent skill precise mode
                       (mirrors Claude Code's agent-level skills whitelist).
    """
    if not skills:
        return ""

    lines = ["The following skills are available for use with the Skill tool:\n"]
    for skill in skills.values():
        if exclude_fork and skill.is_fork:
            continue
        if filter_names is not None and skill.name not in filter_names:
            continue
        desc = skill.description
        if skill.when_to_use:
            desc = f"{desc} - {skill.when_to_use}"
        if len(desc) > _MAX_LISTING_DESC_CHARS:
            desc = desc[: _MAX_LISTING_DESC_CHARS - 1] + "\u2026"
        lines.append(f"- {skill.name}: {desc}")

    # All skills were filtered out
    if len(lines) == 1:
        return ""

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Skill reminder builder — produces the user message for injection
# ---------------------------------------------------------------------------

# Tracks which skills have already been announced to the LLM in this session.
# Mirrors Claude Code's sentSkillNames per-agent tracking in attachments.ts.
_sent_skill_names: set[str] = set()


def build_skill_attachment(
    *,
    force: bool = False,
    exclude_fork: bool = False,
    filter_names: list[str] | None = None,
) -> Attachment | None:
    """Build a <system-reminder> user message listing available skills.

    Called by build_skill_reminder() in messages.py — not intended for direct external use.

    Parameters:
        force: If True, reset sent tracking and return all skills (used after compact).
    """
    global _sent_skill_names

    if force:
        _sent_skill_names = set()

    all_skills = get_skills()
    if not all_skills:
        return None

    new_skills = {
        name: info
        for name, info in all_skills.items()
        if name not in _sent_skill_names
    }

    if not new_skills:
        return None

    listing = format_skill_listing(
        new_skills,
        exclude_fork=exclude_fork,
        filter_names=filter_names,
    )
    if not listing:
        return None

    _sent_skill_names.update(new_skills.keys())

    return Attachment(
        type="system_reminder",
        content=f"<system-reminder>\n{listing}\n</system-reminder>",
    )


def reset_sent_skills() -> None:
    """Clear the sent-skill tracker (e.g. on /clear or session reset).

    After calling this, the next build_skill_attachment() will re-announce
    all skills.
    """
    global _sent_skill_names
    _sent_skill_names = set()


# ---------------------------------------------------------------------------
# Module-level cache for quick access
# ---------------------------------------------------------------------------

_cached_skills: dict[str, SkillInfo] | None = None


def get_skills(force_reload: bool = False) -> dict[str, SkillInfo]:
    """Return the skill registry, loading on first call.

    The cache is intentionally simple — skills don't change during a session.
    Call with force_reload=True after adding skills dynamically.
    """
    global _cached_skills
    if _cached_skills is None or force_reload:
        _cached_skills = discover_skills()
    return _cached_skills


def get_skill(name: str) -> SkillInfo | None:
    """Look up a single skill by name."""
    return get_skills().get(name)
