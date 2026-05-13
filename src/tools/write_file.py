"""Write file tool — create or overwrite a file (async)."""

from __future__ import annotations

import asyncio
import os

from src.types import ToolResult, ToolDef, ToolUseContext
from src.utils.file_state_cache import FileState

SCHEMA: dict = {
    "name": "write_file",
    "description": (
        "Create a new file or overwrite an existing file with the given content. "
        "Use this for writing text files. The parent directory must exist."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The absolute path to the file to write.",
            },
            "content": {
                "type": "string",
                "description": "The content to write to the file.",
            },
        },
        "required": ["file_path", "content"],
    },
}


def _write_sync(file_path: str, content: str, encoding: str = "utf-8") -> int:
    """Blocking I/O helper — returns bytes written."""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    from src.utils.file_encoding import write_text_content
    write_text_content(file_path, content, encoding, "LF")
    return len(content)


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    file_path: str = inputs["file_path"]
    content: str = inputs.get("content", "")

    abs_path = os.path.normpath(os.path.abspath(file_path))
    cache = context.file_state_cache

    # --- stale check: reject if file was externally modified since last read ---
    if cache is not None and os.path.exists(abs_path):
        cached = cache.get(abs_path)
        if not cached or cached.isPartialView:
            return ToolResult(data={
                "type": "error",
                "content": "File has not been read yet. Read it first before writing to it.",
            })
        try:
            disk_mtime = os.path.getmtime(abs_path)
            if disk_mtime > cached.mtime:
                is_full_read = cached.offset is None and cached.limit is None
                content_unchanged = False
                if is_full_read:
                    try:
                        from src.utils.file_encoding import read_file_streaming
                        lines, _, _ = read_file_streaming(abs_path)
                        disk_content = "\n".join(lines)
                        content_unchanged = disk_content == cached.content
                    except Exception:
                        pass
                if not content_unchanged:
                    return ToolResult(data={
                        "type": "error",
                        "content": (
                            "File has been externally modified since last read. "
                            "Read it again before writing."
                        ),
                    })
        except OSError:
            pass

    # Preserve encoding if overwriting an existing file (e.g. UTF-16 LE stays UTF-16 LE).
    # New files default to UTF-8.
    enc = "utf-8"
    if os.path.isfile(abs_path):
        from src.utils.file_encoding import detect_encoding
        enc = await asyncio.to_thread(detect_encoding, abs_path)

    try:
        chars = await asyncio.to_thread(_write_sync, abs_path, content, enc)
    except Exception as e:
        return ToolResult(data={
            "type": "error",
            "content": f"Error writing file: {e}",
        })

    # --- update cache: record written content + new mtime ---
    if cache is not None:
        try:
            mtime = os.path.getmtime(abs_path)
        except OSError:
            mtime = 0.0
        cache.set(abs_path, FileState(
            content=content,
            mtime=mtime,
            offset=None,
            limit=None,
        ))

    return ToolResult(data={
        "type": "success",
        "file_path": file_path,
        "chars_written": chars,
        "content_preview": _make_preview(content),
    })


_PREVIEW_MAX_LINES = 5
_PREVIEW_MAX_LINE_CHARS = 120


def _make_preview(content: str) -> str:
    lines = content.splitlines()
    kept = []
    for line in lines[:_PREVIEW_MAX_LINES]:
        if len(line) > _PREVIEW_MAX_LINE_CHARS:
            kept.append(line[:_PREVIEW_MAX_LINE_CHARS] + "...")
        else:
            kept.append(line)
    remaining = len(lines) - len(kept)
    if remaining > 0:
        kept.append(f"... ({remaining} more lines)")
    return "\n".join(kept)


def map_result(data: dict) -> str:
    if data.get("type") == "error":
        return data["content"]
    return f"Wrote {data['chars_written']} chars to {data['file_path']}"


def display_result(data: dict) -> str:
    if data.get("type") == "error":
        return ""

    header = map_result(data)
    preview = data.get("content_preview", "")
    if not preview:
        return header

    parts = [header, ""]
    for line in preview.splitlines():
        parts.append(f"  {line}")
    return "\n".join(parts)


def is_read_only(inputs: dict) -> bool:
    return False


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    display_result=display_result,
    is_read_only=is_read_only,
)
