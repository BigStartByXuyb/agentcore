"""Tests for Glob tool."""

import os
import tempfile
from unittest import mock

import pytest

from src.tools.glob_tool import tool, SCHEMA, GLOB_RESULT_LIMIT


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_name(self):
        assert SCHEMA["name"] == "glob"

    def test_pattern_required(self):
        assert "pattern" in SCHEMA["input_schema"]["required"]

    def test_path_optional(self):
        assert "path" not in SCHEMA["input_schema"]["required"]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class TestExecutor:
    @pytest.mark.asyncio
    async def test_empty_pattern_returns_no_files(self):
        ctx = mock.MagicMock()
        result = await tool.executor({"pattern": ""}, ctx)
        assert result.data["num_files"] == 0
        assert result.data["filenames"] == []

    @pytest.mark.asyncio
    async def test_nonexistent_dir_returns_no_files(self):
        ctx = mock.MagicMock()
        result = await tool.executor({
            "pattern": "*.py",
            "path": "/nonexistent/dir/that/does/not/exist",
        }, ctx)
        assert result.data["num_files"] == 0

    @pytest.mark.asyncio
    async def test_finds_files_in_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ("a.py", "b.py", "c.txt"):
                (open(os.path.join(tmpdir, name), "w")).close()

            ctx = mock.MagicMock()
            result = await tool.executor({
                "pattern": "*.py",
                "path": tmpdir,
            }, ctx)
            assert result.data["num_files"] == 2
            names = set(result.data["filenames"])
            assert names == {"a.py", "b.py"}

    @pytest.mark.asyncio
    async def test_recursive_glob(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "sub")
            os.makedirs(subdir)
            (open(os.path.join(tmpdir, "root.py"), "w")).close()
            (open(os.path.join(subdir, "nested.py"), "w")).close()

            ctx = mock.MagicMock()
            result = await tool.executor({
                "pattern": "**/*.py",
                "path": tmpdir,
            }, ctx)
            assert result.data["num_files"] == 2
            names = set(result.data["filenames"])
            assert "root.py" in names
            assert "sub/nested.py" in names

    @pytest.mark.asyncio
    async def test_defaults_to_cwd(self):
        ctx = mock.MagicMock()
        result = await tool.executor({"pattern": "*.py"}, ctx)
        assert isinstance(result.data["filenames"], list)

    @pytest.mark.asyncio
    async def test_sorted_by_mtime_newest_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old = os.path.join(tmpdir, "old.py")
            new = os.path.join(tmpdir, "new.py")
            with open(old, "w") as f:
                f.write("old")
            os.utime(old, (1000, 1000))
            with open(new, "w") as f:
                f.write("new")
            os.utime(new, (2000, 2000))

            ctx = mock.MagicMock()
            result = await tool.executor({
                "pattern": "*.py",
                "path": tmpdir,
            }, ctx)
            assert result.data["filenames"][0] == "new.py"
            assert result.data["filenames"][1] == "old.py"

    @pytest.mark.asyncio
    async def test_truncation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(GLOB_RESULT_LIMIT + 5):
                (open(os.path.join(tmpdir, f"file{i:04d}.txt"), "w")).close()

            ctx = mock.MagicMock()
            result = await tool.executor({
                "pattern": "*.txt",
                "path": tmpdir,
            }, ctx)
            assert result.data["truncated"] is True
            assert result.data["num_files"] == GLOB_RESULT_LIMIT

    @pytest.mark.asyncio
    async def test_no_truncation_within_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(3):
                (open(os.path.join(tmpdir, f"f{i}.txt"), "w")).close()

            ctx = mock.MagicMock()
            result = await tool.executor({
                "pattern": "*.txt",
                "path": tmpdir,
            }, ctx)
            assert result.data["truncated"] is False

    @pytest.mark.asyncio
    async def test_skips_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "subdir.py"))
            (open(os.path.join(tmpdir, "file.py"), "w")).close()

            ctx = mock.MagicMock()
            result = await tool.executor({
                "pattern": "*.py",
                "path": tmpdir,
            }, ctx)
            assert result.data["num_files"] == 1
            assert result.data["filenames"] == ["file.py"]


# ---------------------------------------------------------------------------
# map_result
# ---------------------------------------------------------------------------

class TestMapResult:
    def test_no_files(self):
        text = tool.map_result({"filenames": [], "truncated": False})
        assert text == "No files found"

    def test_files_listed(self):
        text = tool.map_result({
            "filenames": ["a.py", "b.py"],
            "truncated": False,
        })
        assert "a.py\nb.py" == text

    def test_truncated_message(self):
        text = tool.map_result({
            "filenames": ["a.py"],
            "truncated": True,
        })
        assert "truncated" in text.lower()
        assert "a.py" in text


# ---------------------------------------------------------------------------
# is_read_only
# ---------------------------------------------------------------------------

class TestIsReadOnly:
    def test_is_read_only(self):
        assert tool.is_read_only({}) is True
