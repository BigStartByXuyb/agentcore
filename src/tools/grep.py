"""Grep tool — search file contents with regex patterns (async)."""

from __future__ import annotations

import asyncio
import shutil

from src.core.types import ToolResult, ToolDef, ToolUseContext

SCHEMA: dict = {
    "name": "grep",
    "description": (
        "Search for a pattern in file contents using regex. "
        "Returns matching lines with file paths and line numbers."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": (
                    "File or directory to search in. "
                    "Defaults to current working directory."
                ),
            },
            "glob": {
                "type": "string",
                "description": (
                    'Glob pattern to filter files (e.g. "*.py", "*.{ts,tsx}").'
                ),
            },
        },
        "required": ["pattern"],
    },
}

_HAS_RG = shutil.which("rg") is not None


def _build_rg_command(pattern: str, path: str | None, glob: str | None) -> list[str]:
    cmd = ["rg", "--line-number", "--no-heading"]
    if glob:
        cmd.extend(["--glob", glob])
    cmd.append(pattern)
    if path:
        cmd.append(path)
    return cmd


def _build_grep_command(pattern: str, path: str | None, glob: str | None) -> list[str]:
    cmd = ["grep", "-rn"]
    if glob:
        cmd.extend(["--include", glob])
    cmd.append(pattern)
    cmd.append(path or ".")
    return cmd


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    pattern: str = inputs["pattern"]
    path: str | None = inputs.get("path")
    glob: str | None = inputs.get("glob")

    if _HAS_RG:
        cmd = _build_rg_command(pattern, path, glob)
    else:
        cmd = _build_grep_command(pattern, path, glob)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return ToolResult(data={
            "num_files": 0,
            "num_matches": 0,
            "content": "Error: neither 'rg' nor 'grep' found on PATH.",
        })

    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return ToolResult(data={
            "num_files": 0,
            "num_matches": 0,
            "content": "Search timed out after 30s.",
        })

    output = stdout_bytes.decode("utf-8", errors="replace").strip()
    if not output:
        return ToolResult(data={
            "num_files": 0,
            "num_matches": 0,
            "content": "",
        })

    lines = output.split("\n")
    files = set()
    for line in lines:
        # Format: "file:line:content" or "file:line-content"
        colon_idx = line.find(":")
        if colon_idx > 0:
            files.add(line[:colon_idx])

    return ToolResult(data={
        "num_files": len(files),
        "num_matches": len(lines),
        "content": output,
    })


def map_result(data: dict) -> str:
    content = data.get("content", "")
    if not content:
        return "No matches found"
    return content


def is_read_only(inputs: dict) -> bool:
    return True


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    is_read_only=is_read_only,
    max_result_size_chars=20_000,
)
