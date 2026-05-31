"""Tests for session persistence (src/session/)."""

import json
import os
import tempfile

import pytest

from src.core.types import Message, MessageHistory, TextContent, ToolResultContent
from src.session.storage import SessionStorage
from src.session.paths import get_session_path


@pytest.fixture
def tmp_sessions_dir(monkeypatch, tmp_path):
    """Override sessions dir to a temp directory."""
    sessions_dir = str(tmp_path / "sessions")
    monkeypatch.setenv("MYAGENT_SESSIONS_DIR", sessions_dir)
    return sessions_dir


class TestSessionStorage:
    def test_open_creates_file_with_header(self, tmp_sessions_dir):
        storage = SessionStorage("test123")
        storage.open()
        # File is lazy — not created until first append
        path = get_session_path("test123")
        assert not os.path.isfile(path)

        storage.append(Message(role="user", content="hi", msg_type="human"))
        storage.close()

        assert os.path.isfile(path)
        with open(path, "r", encoding="utf-8") as f:
            line = json.loads(f.readline())
        assert line["event"] == "session_start"
        assert line["session_id"] == "test123"
        assert "cwd" in line
        assert "timestamp" in line

    def test_append_writes_message(self, tmp_sessions_dir):
        storage = SessionStorage("test456")
        storage.open()

        msg = Message(role="user", content="hello", msg_type="human")
        storage.append(msg)
        storage.close()

        path = get_session_path("test456")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 2  # header + message
        data = json.loads(lines[1])
        assert data["role"] == "user"
        assert data["content"] == "hello"
        assert data["msg_type"] == "human"

    def test_append_filters_system_reminder_attachments(self, tmp_sessions_dir):
        from src.core.types import Attachment
        storage = SessionStorage("test_filter")
        storage.open()

        msg = Message(role="user", content="hi", msg_type="human")
        msg.attach([
            Attachment(type="system_reminder", content="skill listing..."),
            Attachment(type="memory_index", content="memory..."),
            Attachment(type="invoked_skills", content="skill content..."),
        ])
        storage.append(msg)
        storage.close()

        path = get_session_path("test_filter")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        data = json.loads(lines[1])
        # Only invoked_skills should survive
        assert len(data["attachments"]) == 1
        assert data["attachments"][0]["type"] == "invoked_skills"

    def test_append_compaction_marker(self, tmp_sessions_dir):
        storage = SessionStorage("test_compact")
        storage.open()
        storage.append(Message(role="user", content="msg1", msg_type="human"))
        storage.append_compaction_marker()
        storage.append(Message(role="user", content="summary", msg_type="human"))
        storage.close()

        path = get_session_path("test_compact")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # header + msg1 + marker + summary = 4 lines
        assert len(lines) == 4
        assert json.loads(lines[2])["event"] == "compacted"

    def test_load_basic(self, tmp_sessions_dir):
        storage = SessionStorage("test_load")
        storage.open()
        storage.append(Message(role="user", content="hello", msg_type="human"))
        storage.append(Message(role="assistant", content=[TextContent(text="hi")], msg_type="assistant"))
        storage.close()

        history = SessionStorage.load("test_load")
        assert len(history) == 2
        assert history.messages[0].role == "user"
        assert history.messages[0].content == "hello"
        assert history.messages[1].role == "assistant"

    def test_load_respects_compaction_marker(self, tmp_sessions_dir):
        storage = SessionStorage("test_compact_load")
        storage.open()
        storage.append(Message(role="user", content="old msg", msg_type="human"))
        storage.append(Message(role="assistant", content=[TextContent(text="old reply")], msg_type="assistant"))
        storage.append_compaction_marker()
        storage.append(Message(role="user", content="summary", msg_type="human"))
        storage.append(Message(role="user", content="new msg", msg_type="human"))
        storage.close()

        history = SessionStorage.load("test_compact_load")
        assert len(history) == 2
        assert history.messages[0].content == "summary"
        assert history.messages[1].content == "new msg"

    def test_load_file_not_found(self, tmp_sessions_dir):
        with pytest.raises(FileNotFoundError):
            SessionStorage.load("nonexistent")

    def test_last_session_id(self, tmp_sessions_dir):
        import time
        # No sessions yet
        assert SessionStorage.last_session_id() is None

        # Create two sessions (must append to materialize files)
        s1 = SessionStorage("aaa")
        s1.open()
        s1.append(Message(role="user", content="a", msg_type="human"))
        s1.close()

        time.sleep(0.05)

        s2 = SessionStorage("bbb")
        s2.open()
        s2.append(Message(role="user", content="b", msg_type="human"))
        s2.close()

        # bbb was created last
        assert SessionStorage.last_session_id() == "bbb"

    def test_list_sessions(self, tmp_sessions_dir):
        import time
        s1 = SessionStorage("sess1")
        s1.open()
        s1.append(Message(role="user", content="first question", msg_type="human"))
        s1.close()

        time.sleep(0.05)  # ensure different mtime on Windows

        s2 = SessionStorage("sess2")
        s2.open()
        s2.append(Message(role="user", content="second question", msg_type="human"))
        s2.close()

        sessions = SessionStorage.list_sessions()
        assert len(sessions) == 2
        assert sessions[0]["id"] == "sess2"  # newest first
        assert "second question" in sessions[0]["preview"]
