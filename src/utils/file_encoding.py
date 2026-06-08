"""File encoding detection and line-ending handling.

Encoding detection: read the first few bytes for BOM markers.
Line-ending detection: sample first 4KB to count CRLF vs LF.
Read/write contract:
  - Reading always normalizes CRLF → LF (internal representation)
  - Writing restores the original line-ending style
"""

from __future__ import annotations

import os
import stat as stat_mod
from typing import Literal

LineEndingType = Literal["LF", "CRLF"]

# BOM detection only needs a handful of bytes, but we read 4096 to reuse
# the same buffer for line-ending detection (needs enough lines to be meaningful).
_BOM_SAMPLE_SIZE = 4096

# --- Linux special device paths (fixed kernel paths) ---
_BLOCKED_PATHS_LINUX: set[str] = {
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/full",
    "/dev/stdin",
    "/dev/tty",
    "/dev/console",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/fd/0",
    "/dev/fd/1",
    "/dev/fd/2",
}

# --- Windows reserved device names ---
# These work at ANY path and with ANY extension:
#   C:\Users\foo\CON       → console device
#   D:\project\COM1.txt    → serial port
# Only the basename matters (case-insensitive).
_BLOCKED_DEVICE_NAMES_WIN: set[str] = {
    "con", "nul", "aux", "prn",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}


def _is_windows_device_name(file_path: str) -> bool:
    """Check if the basename (without extension) is a Windows reserved device name."""
    basename = os.path.basename(file_path)
    name_without_ext = os.path.splitext(basename)[0]
    return name_without_ext.lower() in _BLOCKED_DEVICE_NAMES_WIN


def is_blocked_path(file_path: str) -> bool:
    """Check if a path is a dangerous device/pipe that would hang or infinite-output.

    Two layers:
      - Linux: exact path match against known /dev/* and /proc/*/fd/* paths
      - Windows: basename match against reserved device names (CON, COM1, etc.)
        These are special because Windows resolves them as devices regardless of
        directory or extension — C:\\any\\path\\CON.txt is still the console.
    """
    # Linux paths
    if file_path in _BLOCKED_PATHS_LINUX:
        return True
    if file_path.startswith("/proc/") and file_path.endswith(("/fd/0", "/fd/1", "/fd/2")):
        return True

    # Windows device names
    if os.name == "nt" and _is_windows_device_name(file_path):
        return True

    return False


def is_special_file(file_path: str) -> bool:
    """Check if path is a FIFO, socket, or device (not a regular file).

    These files can block on read or produce infinite output.
    Returns False if stat fails (file doesn't exist — let caller handle).
    """
    try:
        mode = os.stat(file_path).st_mode
        return (
            stat_mod.S_ISFIFO(mode)
            or stat_mod.S_ISSOCK(mode)
            or stat_mod.S_ISCHR(mode)
            or stat_mod.S_ISBLK(mode)
        )
    except OSError:
        return False


def detect_encoding(file_path: str) -> str:
    """Detect file encoding by reading BOM bytes.

    Returns 'utf-16-le' if UTF-16 LE BOM found, otherwise 'utf-8'.
    Empty files default to 'utf-8' (avoids corruption when writing emoji/CJK later).
    """
    try:
        with open(file_path, "rb") as f:
            head = f.read(_BOM_SAMPLE_SIZE)
    except OSError:
        return "utf-8"

    if len(head) == 0:
        return "utf-8"

    # UTF-16 LE BOM: FF FE
    if len(head) >= 2 and head[0] == 0xFF and head[1] == 0xFE:
        return "utf-16-le"

    # UTF-8 BOM: EF BB BF (still utf-8, just with BOM)
    # No special handling needed — Python's utf-8-sig codec strips it,
    # but we use plain utf-8 and strip manually if needed.

    return "utf-8"


def detect_line_endings(content: str) -> LineEndingType:
    """Detect dominant line-ending style from a string sample.

    Counts CRLF vs lone-LF occurrences; majority wins.
    Default to LF if no newlines found.
    """
    crlf_count = 0
    lf_count = 0

    i = 0
    while i < len(content):
        if content[i] == "\n":
            if i > 0 and content[i - 1] == "\r":
                crlf_count += 1
            else:
                lf_count += 1
        i += 1

    return "CRLF" if crlf_count > lf_count else "LF"


def read_file_with_metadata(file_path: str) -> tuple[str, str, LineEndingType]:
    """Read a file and return (content, encoding, line_endings).

    Content is always CRLF-normalized to LF.
    Encoding and line_endings are detected for write-back preservation.
    """
    encoding = detect_encoding(file_path)

    # newline="" prevents Python from auto-converting CRLF → LF,
    # so we can detect the original line-ending style before normalizing.
    with open(file_path, "r", encoding=encoding, errors="replace", newline="") as f:
        raw = f.read()

    # Detect line endings from raw content (before normalization erases CRLF)
    line_endings = detect_line_endings(raw[:_BOM_SAMPLE_SIZE])

    # Strip UTF-8 BOM if present
    if raw and raw[0] == "﻿":
        raw = raw[1:]

    # Normalize CRLF → LF for internal processing
    content = raw.replace("\r\n", "\n")

    return content, encoding, line_endings


def write_text_content(
    file_path: str,
    content: str,
    encoding: str = "utf-8",
    line_endings: LineEndingType = "LF",
) -> None:
    """Write content to file, restoring the original line-ending style.

    If original was CRLF, converts LF back to CRLF before writing.
    """
    to_write = content
    if line_endings == "CRLF":
        # Normalize any existing CRLF first to avoid \r\r\n
        to_write = content.replace("\r\n", "\n").replace("\n", "\r\n")

    with open(file_path, "w", encoding=encoding, newline="") as f:
        f.write(to_write)


def read_file_streaming(
    file_path: str,
    offset: int = 0,
    limit: int | None = None,
    max_bytes: int | None = None,
) -> tuple[list[str], int, bool]:
    """Read specific line range without loading entire file into memory.

    Uses Python's file iterator (internally buffered, ~8KB chunks).
    Only stores lines within [offset, offset+limit) range.
    BOM is stripped from the first line (consistent with read_file_with_metadata).

    If max_bytes is set, stops selecting lines when adding the next line
    would exceed the byte budget. Truncation always happens at a complete
    line boundary. The stream continues to count total lines even after
    truncation.

    Returns (selected_lines, total_line_count, truncated_by_bytes).
    Lines are CRLF-stripped (rstrip handles \\r\\n and \\n).
    """
    encoding = detect_encoding(file_path)
    selected: list[str] = []
    end_line = offset + limit if limit is not None else float("inf")
    line_index = 0
    accumulated_bytes = 0
    truncated_by_bytes = False
    is_first_line = True

    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        for line in f:
            if is_first_line:
                is_first_line = False
                if line and line[0] == "﻿":
                    line = line[1:]

            if offset <= line_index < end_line and not truncated_by_bytes:
                stripped = line.rstrip("\r\n")
                if max_bytes is not None:
                    sep = 1 if selected else 0
                    line_bytes = len(stripped.encode("utf-8")) + sep
                    if accumulated_bytes + line_bytes > max_bytes:
                        truncated_by_bytes = True
                        line_index += 1
                        continue
                    accumulated_bytes += line_bytes
                selected.append(stripped)
            line_index += 1

    return selected, line_index, truncated_by_bytes
