"""Tests for src/core/settings.py."""
from __future__ import annotations

import json
import os
import warnings

import pytest

from src.core.settings import (
    SettingsSchema,
    _DEFAULT_SETTINGS,
    _read_settings_file,
    ensure_default_settings,
    get_settings_file_paths,
    get_settings_paths,
    load_settings,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Override HOME (and USERPROFILE on Windows) to a temp directory."""
    home_dir = tmp_path / "fakehome"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))  # Windows
    return home_dir


@pytest.fixture
def fake_cwd(tmp_path, monkeypatch):
    """Override os.getcwd() to a temp directory."""
    cwd_dir = tmp_path / "fakecwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(str(cwd_dir))
    return cwd_dir


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Task 2: SettingsSchema + get_settings_paths
# ---------------------------------------------------------------------------

class TestSettingsSchema:
    def test_all_fields_default_to_none(self):
        s = SettingsSchema()
        for field in SettingsSchema.model_fields:
            assert getattr(s, field) is None, f"Expected {field} to be None"

    def test_accepts_known_fields(self):
        s = SettingsSchema(model="claude-opus-4", max_turns=10, thinking_enabled=True)
        assert s.model == "claude-opus-4"
        assert s.max_turns == 10
        assert s.thinking_enabled is True

    def test_ignores_unknown_fields(self):
        # extra="ignore" — should not raise
        s = SettingsSchema.model_validate({"model": "x", "nonexistent_key": "boom"})
        assert s.model == "x"
        assert not hasattr(s, "nonexistent_key")

    def test_list_fields(self):
        s = SettingsSchema(sandbox_allow_write=["/tmp", "/home"])
        assert s.sandbox_allow_write == ["/tmp", "/home"]


class TestGetSettingsPaths:
    def test_returns_two_paths(self, fake_home, fake_cwd):
        user_path, project_path = get_settings_paths()
        assert isinstance(user_path, str)
        assert isinstance(project_path, str)

    def test_user_path_under_home(self, fake_home, fake_cwd):
        user_path, _ = get_settings_paths()
        assert str(fake_home) in user_path
        assert user_path.endswith("settings.json")
        assert ".my-agent" in user_path

    def test_project_path_under_cwd(self, fake_home, fake_cwd):
        _, project_path = get_settings_paths()
        assert str(fake_cwd) in project_path
        assert project_path.endswith("settings.json")
        assert ".my-agent" in project_path

    def test_paths_are_different(self, fake_home, fake_cwd):
        user_path, project_path = get_settings_paths()
        assert user_path != project_path


# ---------------------------------------------------------------------------
# Task 3: _read_settings_file + load_settings
# ---------------------------------------------------------------------------

class TestReadSettingsFile:
    def test_reads_valid_json(self, tmp_path):
        path = str(tmp_path / "settings.json")
        _write_json(path, {"model": "claude-haiku-3", "max_turns": 5})
        s = _read_settings_file(path)
        assert s.model == "claude-haiku-3"
        assert s.max_turns == 5

    def test_returns_empty_for_missing_file(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        s = _read_settings_file(path)
        assert s == SettingsSchema()

    def test_ignores_unknown_fields(self, tmp_path):
        path = str(tmp_path / "settings.json")
        _write_json(path, {"model": "x", "unknown_key": "value"})
        s = _read_settings_file(path)
        assert s.model == "x"

    def test_warns_on_invalid_type(self, tmp_path):
        """Wrong type for a known field → warning + empty schema."""
        path = str(tmp_path / "settings.json")
        _write_json(path, {"max_turns": "not-an-int"})  # max_turns must be int
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            s = _read_settings_file(path)
        assert s == SettingsSchema()
        assert any("Validation error" in str(w.message) for w in caught)

    def test_handles_malformed_json(self, tmp_path):
        path = str(tmp_path / "settings.json")
        with open(path, "w") as f:
            f.write("{not valid json}")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            s = _read_settings_file(path)
        assert s == SettingsSchema()
        assert any("Malformed JSON" in str(w.message) for w in caught)


class TestLoadSettings:
    def test_user_only(self, fake_home, fake_cwd):
        user_path, _ = get_settings_paths()
        _write_json(user_path, {"model": "user-model", "max_turns": 20})
        result = load_settings()
        assert result.model == "user-model"
        assert result.max_turns == 20

    def test_merges_user_and_project(self, fake_home, fake_cwd):
        """Project non-None values override user values."""
        user_path, project_path = get_settings_paths()
        _write_json(user_path, {"model": "user-model", "max_turns": 20, "memory_enabled": True})
        _write_json(project_path, {"model": "project-model", "max_agent_depth": 5})

        result = load_settings()
        # project overrides model
        assert result.model == "project-model"
        # user value preserved when project doesn't set it
        assert result.max_turns == 20
        assert result.memory_enabled is True
        # project adds new field
        assert result.max_agent_depth == 5

    def test_project_does_not_override_with_none(self, fake_home, fake_cwd):
        """Project file without a field should NOT erase user's setting."""
        user_path, project_path = get_settings_paths()
        _write_json(user_path, {"model": "user-model"})
        _write_json(project_path, {"max_turns": 10})  # no model field

        result = load_settings()
        assert result.model == "user-model"
        assert result.max_turns == 10

    def test_no_files_all_none(self, fake_home, fake_cwd):
        """When neither file exists, all fields are None."""
        result = load_settings()
        assert result == SettingsSchema()

    def test_project_overrides_all_user_fields(self, fake_home, fake_cwd):
        user_path, project_path = get_settings_paths()
        _write_json(user_path, {"thinking_enabled": False, "memory_enabled": False})
        _write_json(project_path, {"thinking_enabled": True, "memory_enabled": True})

        result = load_settings()
        assert result.thinking_enabled is True
        assert result.memory_enabled is True


# ---------------------------------------------------------------------------
# Task 4: ensure_default_settings + get_settings_file_paths
# ---------------------------------------------------------------------------

class TestEnsureDefaultSettings:
    def test_creates_file_when_missing(self, fake_home, fake_cwd):
        user_path, _ = get_settings_paths()
        assert not os.path.isfile(user_path)

        ensure_default_settings()

        assert os.path.isfile(user_path)
        with open(user_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Check that non-None _DEFAULT_SETTINGS keys are present
        # (None values are filtered out when writing to avoid null in JSON)
        for key, value in _DEFAULT_SETTINGS.items():
            if value is not None:
                assert data[key] == value
            else:
                assert key not in data

    def test_does_not_overwrite_existing(self, fake_home, fake_cwd):
        user_path, _ = get_settings_paths()
        custom = {"model": "my-custom-model", "max_turns": 99}
        _write_json(user_path, custom)

        ensure_default_settings()  # should be a no-op

        with open(user_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["model"] == "my-custom-model"
        assert data["max_turns"] == 99
        # Default keys NOT injected over custom file
        assert data.get("provider") is None  # custom file has no provider key

    def test_creates_parent_directory(self, fake_home, fake_cwd):
        user_path, _ = get_settings_paths()
        parent = os.path.dirname(user_path)
        # Parent dir should NOT exist yet
        assert not os.path.isdir(parent)

        ensure_default_settings()

        assert os.path.isdir(parent)
        assert os.path.isfile(user_path)


class TestGetSettingsFilePaths:
    def test_returns_both_paths(self, fake_home, fake_cwd):
        paths = get_settings_file_paths()
        assert isinstance(paths, list)
        assert len(paths) == 2

    def test_paths_match_get_settings_paths(self, fake_home, fake_cwd):
        user_path, project_path = get_settings_paths()
        file_paths = get_settings_file_paths()
        assert user_path in file_paths
        assert project_path in file_paths

    def test_returns_absolute_paths(self, fake_home, fake_cwd):
        paths = get_settings_file_paths()
        for p in paths:
            assert os.path.isabs(p), f"Expected absolute path, got: {p}"


# ---------------------------------------------------------------------------
# Task 5: config.py reload from settings
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=False)
def restore_config():
    """Restore config globals after tests that call config.reload()."""
    yield
    from src.core import config
    config.reload()


class TestConfigReload:
    @pytest.fixture(autouse=True)
    def _restore(self, restore_config):
        pass

    def test_reload_picks_up_settings(self, fake_home, fake_cwd):
        user_path, _ = get_settings_paths()
        _write_json(user_path, {"max_tokens": 9999, "thinking_enabled": False, "memory_max_relevant": 10})
        from src.core import config
        config.reload()
        assert config.get().max_tokens == 9999
        assert config.get().thinking_enabled is False
        assert config.get().memory_max_relevant == 10

    def test_env_var_overrides_settings(self, fake_home, fake_cwd, monkeypatch):
        user_path, _ = get_settings_paths()
        _write_json(user_path, {"provider": "deepseek"})
        monkeypatch.setenv("AGENT_PROVIDER", "anthropic")
        from src.core import config
        config.reload()
        assert config.get().provider == "anthropic"

    def test_reload_without_settings_keeps_defaults(self, fake_home, fake_cwd):
        from src.core import config
        config.reload()
        assert config.get().max_tokens == 16384
        assert config.get().thinking_enabled is True

    def test_reload_max_turns(self, fake_home, fake_cwd):
        user_path, _ = get_settings_paths()
        _write_json(user_path, {"max_turns": 99})
        from src.core import config
        config.reload()
        assert config.get().max_turns == 99

    def test_reload_memory_settings(self, fake_home, fake_cwd):
        user_path, _ = get_settings_paths()
        _write_json(user_path, {"memory_enabled": False, "memory_max_files": 50})
        from src.core import config
        config.reload()
        assert config.get().memory_enabled is False
        assert config.get().memory_max_files == 50

    def test_reload_compact_settings(self, fake_home, fake_cwd):
        user_path, _ = get_settings_paths()
        _write_json(user_path, {"micro_compact_enabled": False, "auto_compact_max_tokens": 2048})
        from src.core import config
        config.reload()
        assert config.get().micro_compact_enabled is False
        assert config.get().auto_compact_max_tokens == 2048

    def test_reload_sandbox_settings(self, fake_home, fake_cwd):
        user_path, _ = get_settings_paths()
        _write_json(user_path, {"sandbox_enabled": False, "sandbox_allow_write": ["/tmp/extra"]})
        from src.core import config
        config.reload()
        assert config.get().sandbox_enabled is False
        assert config.get().sandbox_allow_write == ["/tmp/extra"]

    def test_project_overrides_user_on_reload(self, fake_home, fake_cwd):
        user_path, project_path = get_settings_paths()
        _write_json(user_path, {"max_tokens": 8192})
        _write_json(project_path, {"max_tokens": 4096})
        from src.core import config
        config.reload()
        assert config.get().max_tokens == 4096

    def test_session_id_unchanged_by_reload(self, fake_home, fake_cwd):
        from src.core import config
        original = config.SESSION_ID
        config.reload()
        assert config.SESSION_ID == original

    def test_get_models_returns_current_models(self, fake_home, fake_cwd):
        from src.core import config
        config.reload()
        models = config.get().models
        assert models is config.get().models


# ---------------------------------------------------------------------------
# Task 8: Integration test — end-to-end settings flow
# ---------------------------------------------------------------------------

class TestIntegration:
    @pytest.fixture(autouse=True)
    def _restore(self, restore_config):
        pass

    def test_full_flow(self, fake_home, monkeypatch, tmp_path):
        """End-to-end: ensure defaults → load → modify → reload."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        from src.core.settings import ensure_default_settings, load_settings
        from src.core import config

        # 1. Create defaults
        ensure_default_settings()
        s = load_settings()
        assert s.model == "claude-sonnet-4-6"

        # 2. Apply to config
        config.reload()
        assert config.get().thinking_enabled is True

        # 3. Override with project settings
        proj_settings_dir = project_dir / ".my-agent"
        proj_settings_dir.mkdir()
        (proj_settings_dir / "settings.json").write_text(json.dumps({
            "max_turns": 50,
            "thinking_enabled": False,
        }))

        # 4. Reload picks up project override
        config.reload()
        assert config.get().max_turns == 50
        assert config.get().thinking_enabled is False

        # 5. User-level defaults still preserved for non-overridden fields
        assert config.get().memory_enabled is True
