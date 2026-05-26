"""Bash tool — execute shell commands (async)."""

from __future__ import annotations

import asyncio
import shutil
import re

from src.core.types import ToolResult, ToolDef, ToolUseContext
from src.sandbox import sandbox_manager

SCHEMA: dict = {
    "name": "bash",
    "description": (
        "Execute a shell command and return its output. "
        "Use this for running programs, listing files, installing packages, "
        "and any other system operations."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Optional timeout in milliseconds (default: 120000).",
            },
        },
        "required": ["command"],
    },
}

# Detect available shell once at import time.
# On Windows, prefer Git Bash over WSL bash (C:\Windows\System32\bash.exe)
# to avoid accidentally operating on the WSL filesystem.
_BASH_PATH = (
    shutil.which("bash", path=r"C:\Program Files\Git\usr\bin")
    or shutil.which("bash")
)
_HAS_BASH = _BASH_PATH is not None

DEFAULT_TIMEOUT_MS = 120_000


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    command: str = inputs["command"]
    timeout_ms: int = inputs.get("timeout", DEFAULT_TIMEOUT_MS)
    timeout_sec = timeout_ms / 1000

    if _HAS_BASH:
        shell_cmd = [_BASH_PATH, "-c", command]
    else:
        shell_cmd = ["cmd", "/c", command]

    if sandbox_manager.is_enabled() and not sandbox_manager.is_command_excluded(command):
        shell_cmd = sandbox_manager.wrap_command(shell_cmd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *shell_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            # Best effort: kill the runaway process before returning
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return ToolResult(data={
                "stdout": "",
                "stderr": f"Command timed out after {timeout_sec}s",
                "exit_code": -1,
                "interrupted": True,
            })

        return ToolResult(data={
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            "exit_code": proc.returncode,
            "interrupted": False,
        })
    except Exception as e:
        return ToolResult(data={
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
            "interrupted": False,
        })


def map_result(data: dict) -> str:
    parts: list[str] = []

    exit_code = data.get("exit_code", 0)
    if exit_code != 0:
        parts.append(f"Exit code: {exit_code}")

    stdout = data.get("stdout", "")
    stderr = data.get("stderr", "")

    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)

    if data.get("interrupted"):
        parts.append("[Command was interrupted]")

    return "\n".join(parts) if parts else "(no output)"


# Commands that only read state and never modify the filesystem or system.
_READ_ONLY_COMMANDS = frozenset({
    "ls", "dir", "cat", "head", "tail", "less", "more",
    "echo", "printf", "pwd", "env", "printenv", "whoami", "hostname",
    "date", "cal", "uptime", "uname",
    "wc", "stat", "file", "which", "whereis", "type",
    "find", "grep", "rg", "ag", "ack", "locate",
    "tree", "du", "df", "free",
    "git status", "git log", "git diff", "git show", "git branch",
    "git remote", "git tag",
    "python --version", "node --version", "pip list", "pip show",
})


def _get_base_command(command: str) -> str:
    """Extract the first token (base command) from a shell command string."""
    stripped = command.strip()
    # Handle leading env vars like KEY=val cmd
    for token in stripped.split():
        if "=" not in token:
            return token
    return stripped.split()[0] if stripped else ""


def is_read_only(inputs: dict) -> bool:
    """Check if the command is read-only based on a whitelist approach.

    Splits on |, &&, ;, || and checks every segment.
    """
    command: str = inputs.get("command", "")
    stripped = command.strip()
    if not stripped:
        return True

    # Split on shell operators first — every segment must be read-only
    segments = re.split(r"\|{1,2}|&&|;", stripped)

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue

        # Check full command (for multi-word like "git status")
        if seg in _READ_ONLY_COMMANDS:
            continue

        # Check base command
        base = _get_base_command(seg)
        if base in _READ_ONLY_COMMANDS:
            continue

        return False

    return True


def is_destructive(inputs: dict) -> bool:
    """True for commands that perform irreversible operations."""
    command: str = inputs.get("command", "")
    base = _get_base_command(command)
    return base in _DESTRUCTIVE_COMMANDS


_DESTRUCTIVE_COMMANDS = frozenset({
    "rm", "rmdir", "del",
    "mv", "rename",
    "chmod", "chown",
    "mkfs", "dd",
    "git push", "git reset",
})


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    is_read_only=is_read_only,
    is_destructive=is_destructive,
    max_result_size_chars=30_000,
)
