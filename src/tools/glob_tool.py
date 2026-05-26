"""Glob tool — fast file pattern matching by name.

Corresponds to Claude Code's GlobTool. Uses ripgrep (rg --files --glob)
for high-performance file search, with pathlib fallback if rg is not
available.

Unlike grep (searches file content), glob only matches file paths —
it never opens files, so it's very fast even on large codebases.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from src.core.types import ToolDef, ToolResult, ToolUseContext

logger = logging.getLogger(__name__)

GLOB_RESULT_LIMIT = 100

SCHEMA: dict = {
    "name": "glob",
    "description": (
        "- Fast file pattern matching tool that works with any codebase size\n"
        "- Supports glob patterns like \"**/*.py\" or \"src/**/*.ts\"\n"
        "- Returns matching file paths sorted by modification time\n"
        "- Use this tool when you need to find files by name patterns\n"
        "- When you are doing an open ended search that may require multiple "
        "rounds of globbing and grepping, use the Agent tool instead"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The glob pattern to match files against.",
            },
            "path": {
                "type": "string",
                "description": (
                    "The directory to search in. Defaults to the current working "
                    "directory if not specified."
                ),
            },
        },
        "required": ["pattern"],
    },
}

_rg_path: str | None = shutil.which("rg")


async def _glob_via_ripgrep(pattern: str, search_dir: str) -> list[str]:
    """Use ripgrep to list files matching a glob pattern.

    Runs: rg --files --glob <pattern> --sort=modified <search_dir>
    Returns list of relative file paths (oldest first — rg sorts ascending).
    """
    args = [
        _rg_path or "rg",
        "--files",
        "--glob", pattern,
        "--sort=modified",
        "--no-ignore",
        "--hidden",
        search_dir,
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()

    if not stdout:
        return []

    lines = stdout.decode("utf-8", errors="replace").splitlines()
    search_path = Path(search_dir)
    result: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rel = Path(line).relative_to(search_path).as_posix()
        except ValueError:
            rel = line
        result.append(rel)
    return result


def _glob_via_pathlib(pattern: str, search_dir: str) -> list[tuple[float, str]]:
    """Fallback: use pathlib.Path.glob() with mtime sorting."""
    search_path = Path(search_dir)
    matches: list[tuple[float, str]] = []
    for p in search_path.glob(pattern):
        if not p.is_file():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        try:
            rel = p.relative_to(search_path).as_posix()
        except ValueError:
            rel = str(p)
        matches.append((mtime, rel))
    matches.sort(key=lambda x: x[0], reverse=True)
    return matches


async def executor(inputs: dict, _context: ToolUseContext) -> ToolResult:
    pattern: str = inputs.get("pattern", "")
    empty = {"num_files": 0, "filenames": [], "truncated": False, "duration_ms": 0}

    if not pattern:
        return ToolResult(data=empty)

    search_dir = inputs.get("path") or os.getcwd()
    if not Path(search_dir).is_dir():
        return ToolResult(data=empty)

    start = time.monotonic()

    try:
        if _rg_path:
            # ripgrep returns oldest-first (--sort=modified ascending)
            raw = await _glob_via_ripgrep(pattern, search_dir)
            # reverse to newest-first
            raw.reverse()
            truncated = len(raw) > GLOB_RESULT_LIMIT
            filenames = raw[:GLOB_RESULT_LIMIT]
        else:
            logger.debug("ripgrep not found, falling back to pathlib.glob()")
            matches = _glob_via_pathlib(pattern, search_dir)
            truncated = len(matches) > GLOB_RESULT_LIMIT
            filenames = [name for _, name in matches[:GLOB_RESULT_LIMIT]]
    except OSError:
        return ToolResult(data=empty)

    duration_ms = int((time.monotonic() - start) * 1000)

    return ToolResult(data={
        "num_files": len(filenames),
        "filenames": filenames,
        "truncated": truncated,
        "duration_ms": duration_ms,
    })


def map_result(data: dict) -> str:
    filenames: list[str] = data.get("filenames", [])
    if not filenames:
        return "No files found"
    parts = list(filenames)
    if data.get("truncated"):
        parts.append(
            "(Results are truncated. Consider using a more specific path or pattern.)"
        )
    return "\n".join(parts)


def is_read_only(_inputs: dict) -> bool:
    return True


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    is_read_only=is_read_only,
)
