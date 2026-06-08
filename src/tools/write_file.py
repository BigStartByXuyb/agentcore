"""Write file tool — create or overwrite a file (async)."""

from __future__ import annotations

import os

from src.core.types import ToolResult, ToolDef, ToolUseContext
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


class _StaleFileError(Exception):
    pass


def _check_and_write_sync(
    abs_path: str,
    content: str,
    cached_mtime: float | None,
    cached_content: str | None,
    cached_is_full_read: bool,
) -> tuple[int, float]:
    """Critical section: stale check + detect encoding + write. Runs in one thread.

    No async yield between check and write — prevents TOCTOU race.
    """
    from src.utils.file_encoding import write_text_content

    if cached_mtime is not None and os.path.exists(abs_path):
        try:
            disk_mtime = os.path.getmtime(abs_path)
            if disk_mtime > cached_mtime:
                content_unchanged = False
                if cached_is_full_read and cached_content is not None:
                    try:
                        from src.utils.file_encoding import read_file_streaming
                        lines, _, _ = read_file_streaming(abs_path)
                        content_unchanged = "\n".join(lines) == cached_content
                    except Exception:
                        pass
                if not content_unchanged:
                    raise _StaleFileError(
                        "File has been externally modified since last read. "
                        "Read it again before writing."
                    )
        except OSError:
            pass

    enc = "utf-8"
    if os.path.isfile(abs_path):
        from src.utils.file_encoding import detect_encoding
        enc = detect_encoding(abs_path)

    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    write_text_content(abs_path, content, enc, "LF")

    new_mtime = os.path.getmtime(abs_path)
    return len(content), new_mtime


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    file_path: str = inputs["file_path"]
    content: str = inputs.get("content", "")

    abs_path = os.path.normpath(os.path.abspath(file_path))
    cache = context.file_state_cache

    # Fast pre-check: must have been read first.
    # mtime + content comparison is done inside the critical section
    # (which also has content fallback for Windows mtime false positives).
    cached_mtime: float | None = None
    cached_content: str | None = None
    cached_is_full_read = False
    if cache is not None and os.path.exists(abs_path):
        cached = cache.get(abs_path)
        if not cached or cached.isPartialView:
            return ToolResult(data={
                "type": "error",
                "content": "File has not been read yet. Read it first before writing to it.",
            })
        cached_mtime = cached.mtime
        cached_content = cached.content
        cached_is_full_read = cached.offset is None and cached.limit is None

    # Critical section: stale check + write, fully synchronous (no await).
    # Blocks the event loop briefly (<1ms for typical files), but guarantees
    # no other coroutine can interleave between check and write.
    try:
        chars, new_mtime = _check_and_write_sync(
            abs_path, content,
            cached_mtime, cached_content, cached_is_full_read,
        )
    except _StaleFileError as e:
        return ToolResult(data={"type": "error", "content": str(e)})
    except Exception as e:
        return ToolResult(data={"type": "error", "content": f"Error writing file: {e}"})

    if cache is not None:
        cache.set(abs_path, FileState(
            content=content, mtime=new_mtime, offset=None, limit=None,
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
    return ""


def build_preview(tool_input: dict) -> str:
    """Build a permission preview for write_file."""
    file_path = tool_input.get("file_path", "?")
    content = tool_input.get("content", "")
    exists = os.path.isfile(file_path)

    action = "Overwrite" if exists else "Create"
    header = f"\n  {action}: {file_path}"

    preview_lines = content.splitlines()[:20]
    preview = "\n".join(f"  + {line}" for line in preview_lines)
    if len(content.splitlines()) > 20:
        preview += f"\n  ... ({len(content.splitlines()) - 20} more lines)"

    return header + "\n" + preview + "\n"


def is_read_only(inputs: dict) -> bool:
    return False


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    display_result=display_result,
    build_preview=build_preview,
    is_read_only=is_read_only,
)
