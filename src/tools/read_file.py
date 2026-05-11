"""Read file tool — read file contents with line numbers (async)."""

from __future__ import annotations

import asyncio
import os

from src.types import ToolResult, ToolDef, ToolUseContext
from src.file_state_cache import FileState
from src import config

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
MAX_FILE_SIZE_BYTES = 256 * 1024  # 256 KB — reject full-read if file exceeds this
MAX_OUTPUT_TOKENS = 25_000        # single read token budget

FILE_UNCHANGED_STUB = (
    "File unchanged since last read. "
    "The content from the earlier Read tool_result in this conversation is still current."
)


def _read_lines_sync(file_path: str) -> list[str]:
    """Blocking I/O helper — called via asyncio.to_thread."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    file_path: str = inputs["file_path"]
    offset: int = inputs.get("offset", 0)
    limit: int = inputs.get("limit", DEFAULT_LIMIT)

    abs_path = os.path.normpath(os.path.abspath(file_path))

    if not os.path.exists(abs_path):
        return ToolResult(data={
            "type": "error",
            "content": f"File not found: {file_path}",
            "total_lines": 0,
        })

    if not os.path.isfile(abs_path):
        return ToolResult(data={
            "type": "error",
            "content": f"Not a file: {file_path}",
            "total_lines": 0,
        })

    # --- file size gate (only for full reads without explicit offset/limit) ---
    explicit_range = "offset" in inputs or "limit" in inputs
    if not explicit_range:
        try:
            file_size = os.path.getsize(abs_path)
            if file_size > MAX_FILE_SIZE_BYTES:
                return ToolResult(data={
                    "type": "error",
                    "content": (
                        f"File content ({file_size:,} bytes) exceeds maximum allowed size "
                        f"({MAX_FILE_SIZE_BYTES:,} bytes). "
                        "Use offset and limit parameters to read specific portions of the file, "
                        "or use grep to search for specific content instead of reading the whole file."
                    ),
                    "total_lines": 0,
                })
        except OSError:
            pass

    # --- dedup check ---
    cache = context.file_state_cache
    if cache:
        cached = cache.get(abs_path)
        if (cached
                and cached.offset is not None
                and cached.offset == offset
                and cached.limit == limit):
            try:
                current_mtime = os.path.getmtime(abs_path)
                if current_mtime == cached.mtime:
                    return ToolResult(data={"type": "file_unchanged", "file_path": file_path})
            except OSError:
                pass

    # --- normal read ---
    try:
        all_lines = await asyncio.to_thread(_read_lines_sync, abs_path)
    except Exception as e:
        return ToolResult(data={
            "type": "error",
            "content": f"Error reading file: {e}",
            "total_lines": 0,
        })

    total_lines = len(all_lines)
    selected = all_lines[offset : offset + limit]

    numbered = []
    for i, line in enumerate(selected, start=offset + 1):
        numbered.append(f"{i}\t{line.rstrip()}")

    content = "\n".join(numbered)

    # --- output token estimate gate ---
    estimated_tokens = config.estimate_tokens(content)
    if estimated_tokens > MAX_OUTPUT_TOKENS:
        return ToolResult(data={
            "type": "error",
            "content": (
                f"File output (~{estimated_tokens:,} tokens) exceeds maximum allowed "
                f"({MAX_OUTPUT_TOKENS:,} tokens). "
                "Use offset and limit to read a smaller range, "
                "or use grep to search for specific content."
            ),
            "total_lines": total_lines,
        })

    truncated = (offset + limit) < total_lines

    # --- update cache ---
    if cache:
        try:
            mtime = os.path.getmtime(abs_path)
        except OSError:
            mtime = 0.0
        cache.set(abs_path, FileState(content=content, mtime=mtime, offset=offset, limit=limit))

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

    if data.get("type") == "file_unchanged":
        return FILE_UNCHANGED_STUB

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
    max_result_size_chars=999_999_999,
)
