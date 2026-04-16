"""Frontmatter parsing — shared by Skills and Agents.

Extracted from src/skills/__init__.py so that both skill and agent
modules can import without cross-dependency.

Mirrors Claude Code's parseFrontmatter() in frontmatterParser.ts.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"^---\s*\n([\s\S]*?)---\s*\n?")


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter_dict, body_content).

    Returns ({}, raw) if no frontmatter block is found.
    """
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw

    fm_text = m.group(1) or ""
    body = raw[m.end():]

    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}, raw

    if not isinstance(parsed, dict):
        return {}, raw

    return parsed, body
