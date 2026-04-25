"""Write file tool — create or overwrite a file (async)."""

from __future__ import annotations

import asyncio
import os

from src.types import ToolResult, ToolDef, ToolUseContext

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


def _write_sync(file_path: str, content: str) -> int:
    """Blocking I/O helper — returns bytes written."""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    return len(content)


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    file_path: str = inputs["file_path"]
    content: str = inputs.get("content", "")

    try:
        chars = await asyncio.to_thread(_write_sync, file_path, content)
    except Exception as e:
        return ToolResult(data={
            "type": "error",
            "content": f"Error writing file: {e}",
        })

    return ToolResult(data={
        "type": "success",
        "file_path": file_path,
        "chars_written": chars,
    })


def map_result(data: dict) -> str:
    if data.get("type") == "error":
        return data["content"]
    return f"Wrote {data['chars_written']} chars to {data['file_path']}"


def is_read_only(inputs: dict) -> bool:
    return False


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    is_read_only=is_read_only,
)
