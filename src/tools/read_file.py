"""Read file tool — read file contents with line numbers (async).

Encoding detection: auto-detect UTF-16 LE (BOM) vs UTF-8 (default).
Line endings: CRLF normalized to LF on read; original style recorded for edit write-back.
Large files: streaming line-by-line read — only requested range held in memory.
Special files: pipes, devices, sockets blocked to prevent hangs.
"""

from __future__ import annotations

import asyncio
import os

from src.core.types import ToolResult, ToolDef, ToolUseContext
from src.utils.file_state_cache import FileState
from src.utils.file_encoding import (
    is_blocked_path,
    is_special_file,
    read_file_streaming,
)
from src.core import config

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
                    "Maximum number of lines to read. "
                    "Only provide if the file is too large to read at once."
                ),
            },
        },
        "required": ["file_path"],
    },
}

MAX_READ_BYTES = 256 * 1024       # 256 KB — byte budget for selected output
MAX_OUTPUT_TOKENS = 25_000        # single read token budget


FILE_UNCHANGED_STUB = (
    "File unchanged since last read. "
    "The content from the earlier Read tool_result in this conversation is still current."
)


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    file_path: str = inputs["file_path"]
    offset: int | None = inputs.get("offset", None)
    limit: int | None = inputs.get("limit", None)
    abs_path = os.path.normpath(os.path.abspath(file_path))

    # --- blocked device/pipe paths (no I/O, just path check) ---
    if is_blocked_path(abs_path):
        return ToolResult(data={
            "type": "error",
            "content": f"Cannot read '{file_path}': this device file would block or produce infinite output.",
            "total_lines": 0,
        })

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

    # --- special file check (FIFO, socket, device) ---
    if is_special_file(abs_path):
        return ToolResult(data={
            "type": "error",
            "content": f"Cannot read '{file_path}': special file (pipe/socket/device) may block or produce infinite output.",
            "total_lines": 0,
        })

    # --- dedup check ---
    cache = context.file_state_cache
    if cache is not None:
        cached = cache.get(abs_path)
        if (cached
                and cached.offset == offset
                and cached.limit == limit):
            try:
                current_mtime = os.path.getmtime(abs_path)
                if current_mtime == cached.mtime:
                    return ToolResult(data={"type": "file_unchanged", "file_path": file_path})
            except OSError:
                pass

    # --- large file rejection (full read only) ---
    is_full_read = offset is None and limit is None
    if is_full_read:
        try:
            file_size = os.path.getsize(abs_path)
            if file_size > MAX_READ_BYTES:
                return ToolResult(data={
                    "type": "error",
                    "content": (
                        f"File is too large to read entirely "
                        f"({file_size:,} bytes, limit is {MAX_READ_BYTES // 1024} KB). "
                        "Use offset and limit to read specific portions of the file, "
                        "or use grep to search for specific content."
                    ),
                    "total_lines": 0,
                })
        except OSError:
            pass

    # --- read file content ---
    actual_offset = offset if offset is not None else 0

    try:
        selected, total_lines, _ = (
            await asyncio.to_thread(
                read_file_streaming, abs_path, actual_offset, limit,
            )
        )
    except Exception as e:
        return ToolResult(data={
            "type": "error",
            "content": f"Error reading file: {e}",
            "total_lines": 0,
        })

    cache_content = "\n".join(selected)

    # --- format with line numbers (for LLM display) ---
    numbered = []
    for i, line in enumerate(selected, start=actual_offset + 1):
        numbered.append(f"{i}\t{line}")
    content = "\n".join(numbered)

    # --- output token estimate gate ---
    estimated_tokens = config.get().estimate_tokens(content)
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

    truncated = limit is not None and (actual_offset + limit) < total_lines

    # --- update cache ---
    if cache is not None:
        try:
            mtime = os.path.getmtime(abs_path)
        except OSError:
            mtime = 0.0
        cache.set(abs_path, FileState(content=cache_content, mtime=mtime, offset=offset, limit=limit))

    return ToolResult(data={
        "type": "text",
        "content": content,
        "total_lines": total_lines,
        "truncated": truncated,
        "start_line": actual_offset + 1,
        "end_line": actual_offset + len(selected),
    })


def map_result(data: dict) -> str:
    if data.get("type") == "error":
        return data["content"]

    if data.get("type") == "file_unchanged":
        return FILE_UNCHANGED_STUB

    content = data.get("content", "")
    if not content:
        return "(empty file)"

    total = data["total_lines"]
    end = data["end_line"]
    remaining = total - end

    if data.get("truncated"):
        content += f"\n\n... ({remaining} more lines remaining. Use offset to read more.)"

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
