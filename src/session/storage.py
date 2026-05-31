"""Session persistence — JSONL append-only storage.

WAL pattern: write-only during runtime, read-only on resume.
Each message is appended as one JSON line. Compaction markers
indicate where to start reading on resume.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import IO

from src.core.types import Message, MessageHistory
from src.session.paths import get_sessions_dir, get_session_path, ensure_sessions_dir

logger = logging.getLogger(__name__)

# Attachment types that are regenerated on resume — no need to persist.
_SKIP_ATTACHMENT_TYPES = frozenset({"system_reminder", "memory_index"})


class SessionStorage:
    """Append-only JSONL session persistence."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._path = get_session_path(session_id)
        self._file: IO[str] | None = None
        self._materialized: bool = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def path(self) -> str:
        return self._path

    def open(self) -> None:
        """Mark storage as ready. File is not created until first message (lazy)."""
        ensure_sessions_dir()
        self._materialized = False

    def _materialize(self) -> None:
        """Create the file on disk and write session_start header."""
        if self._materialized:
            return
        self._file = open(self._path, "a", encoding="utf-8")
        self._write_line({
            "event": "session_start",
            "session_id": self._session_id,
            "cwd": os.getcwd(),
            "timestamp": time.time(),
        })
        self._materialized = True

    def append(self, message: Message) -> None:
        """Append a single message as one JSONL line. Creates file on first call."""
        if not self._materialized:
            self._materialize()
        data = message.to_serializable()
        # Filter out attachments that are regenerated on resume
        if data.get("attachments"):
            data["attachments"] = [
                a for a in data["attachments"]
                if a.get("type") not in _SKIP_ATTACHMENT_TYPES
            ]
        self._write_line(data)

    def append_compaction_marker(self) -> None:
        """Write a compaction marker — reader discards all lines before this."""
        if not self._materialized:
            self._materialize()
        self._write_line({
            "event": "compacted",
            "timestamp": time.time(),
        })

    def close(self) -> None:
        """Flush and close the file handle."""
        if self._file:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def _write_line(self, data: dict) -> None:
        """Write a single JSON line and flush."""
        assert self._file is not None
        self._file.write(json.dumps(data, ensure_ascii=False) + "\n")
        self._file.flush()

    # ------------------------------------------------------------------
    # Static methods — reading / listing
    # ------------------------------------------------------------------

    @staticmethod
    def load(session_id: str) -> MessageHistory:
        """Read a session JSONL and rebuild MessageHistory.

        Single-pass streaming: accumulates lines into a buffer, clears
        the buffer on compaction markers. At EOF, only parses the buffer
        (which contains only post-boundary lines).
        """
        path = get_session_path(session_id)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Session file not found: {path}")

        # Single pass: stream lines, clear buffer on boundary
        buffer: list[str] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if '"compacted"' in line and '"event"' in line:
                    buffer.clear()
                else:
                    buffer.append(line)

        # Parse only the surviving lines
        messages: list[Message] = []
        for raw_line in buffer:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSONL line in %s", path)
                continue

            if "event" in data:
                continue
            if "role" not in data:
                continue

            messages.append(Message.from_serializable(data))

        return MessageHistory(messages=messages)

    @staticmethod
    def last_session_id() -> str | None:
        """Return the most recently modified session ID, or None."""
        sessions_dir = get_sessions_dir()
        if not os.path.isdir(sessions_dir):
            return None

        latest_mtime = 0.0
        latest_id: str | None = None
        for fname in os.listdir(sessions_dir):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(sessions_dir, fname)
            mtime = os.path.getmtime(fpath)
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_id = fname[:-6]  # strip .jsonl

        return latest_id

    @staticmethod
    def list_sessions(limit: int = 20) -> list[dict]:
        """List available sessions sorted by mtime (newest first).

        Returns list of {id, path, mtime, preview}.
        """
        sessions_dir = get_sessions_dir()
        if not os.path.isdir(sessions_dir):
            return []

        entries: list[dict] = []
        for fname in os.listdir(sessions_dir):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(sessions_dir, fname)
            sid = fname[:-6]
            mtime = os.path.getmtime(fpath)
            preview = _read_first_user_message(fpath)
            entries.append({
                "id": sid,
                "path": fpath,
                "mtime": mtime,
                "preview": preview,
            })

        entries.sort(key=lambda e: e["mtime"], reverse=True)
        return entries[:limit]


def _read_first_user_message(path: str) -> str:
    """Read the first user message from a JSONL file for preview."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    data = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if data.get("role") == "user" and data.get("msg_type") == "human":
                    content = data.get("content", "")
                    if isinstance(content, str):
                        return content[:80]
                    # list content — find first text block
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            return block.get("text", "")[:80]
                    return "(no text)"
    except OSError:
        pass
    return "(empty)"
