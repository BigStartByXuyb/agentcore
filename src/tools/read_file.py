"""Read file tool — read file contents with line numbers (async)."""

from __future__ import annotations

import asyncio
import os

from src.types import ToolResult, ToolDef, ToolUseContext

SCHEMA: dict = {
    "name": "read_file",
    "description": (
        "Read the contents of a file. Returns content with line numbers. "
        "Use offset and limit to read specific portions of large files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The absolute path to the file to read.",
            },
            "offset": {
                "type": "integer",
                "description": (
                    "Line number to start reading from (0-based). "
                    "Only provide if the file is too large to read at once."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Maximum number of lines to read. Defaults to 2000."
                ),
            },
        },
        "required": ["file_path"],
    },
}

DEFAULT_LIMIT = 2000


def _read_lines_sync(file_path: str) -> list[str]:
    """Blocking I/O helper — called via asyncio.to_thread."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    file_path: str = inputs["file_path"]
    offset: int = inputs.get("offset", 0)
    limit: int = inputs.get("limit", DEFAULT_LIMIT)

    if not os.path.exists(file_path):
        return ToolResult(data={
            "type": "error",
            "content": f"File not found: {file_path}",
            "total_lines": 0,
        })

    if not os.path.isfile(file_path):
        return ToolResult(data={
            "type": "error",
            "content": f"Not a file: {file_path}",
            "total_lines": 0,
        })

    try:
        all_lines = await asyncio.to_thread(_read_lines_sync, file_path)
    except Exception as e:
        return ToolResult(data={
            "type": "error",
            "content": f"Error reading file: {e}",
            "total_lines": 0,
        })

    total_lines = len(all_lines)
    selected = all_lines[offset : offset + limit]

    # Add line numbers (1-based, matching cat -n format)
    numbered = []
    for i, line in enumerate(selected, start=offset + 1):
        numbered.append(f"{i}\t{line.rstrip()}")

    content = "\n".join(numbered)
    truncated = (offset + limit) < total_lines

    return ToolResult(data={
        "type": "text",
        "content": content,
        "total_lines": total_lines,
        "truncated": truncated,
        "start_line": offset + 1,
        "end_line": offset + len(selected),
    })


def map_result(data: dict) -> str:
    if data.get("type") == "error":
        return data["content"]

    content = data.get("content", "")
    if not content:
        return "(empty file)"

    if data.get("truncated"):
        total = data["total_lines"]
        end = data["end_line"]
        content += f"\n\n... ({total - end} more lines remaining. Use offset to read more.)"

    return content


def is_read_only(inputs: dict) -> bool:
    return True


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    is_read_only=is_read_only,
    # Output bounded by limit param; persisting creates circular Read→file→Read loop.
    # Matches Claude Code FileReadTool: maxResultSizeChars = Infinity
    max_result_size_chars=999_999_999,
)
