# Settings Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Externalize hardcoded config values to `settings.json` files with pydantic validation and watchdog hot-reload.

**Architecture:** New `src/core/settings.py` handles file discovery, JSON reading, two-layer merge (user → project), and pydantic validation. Existing `config.py` gains a `reload()` function that re-reads settings and refreshes module-level variables. `watcher.py` gets a settings file watcher that calls `config.reload()` on change.

**Tech Stack:** pydantic v2, watchdog (existing), pytest

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/core/settings.py` | Settings schema, file reading, merging, validation, path resolution |
| Modify | `src/core/config.py` | Init from settings, `reload()`, env-var override layer |
| Modify | `src/watcher.py` | Add settings file watcher |
| Modify | `src/sandbox.py` | Invalidate cached config on reload |
| Modify | `pyproject.toml` | Add pydantic dependency |
| Create | `tests/test_settings.py` | Unit tests for settings + config reload |

---

### Task 1: Add pydantic dependency

**Files:**
- Modify: `pyproject.toml:5`

- [ ] **Step 1: Add pydantic to dependencies**

```toml
dependencies = ["anthropic>=0.90.0", "openai>=1.0.0", "pyyaml>=6.0", "watchdog>=4.0", "pydantic>=2.0"]
```

- [ ] **Step 2: Install**

Run: `pip install -e .`
Expected: pydantic installed successfully

- [ ] **Step 3: Verify import**

Run: `python -c "import pydantic; print(pydantic.__version__)"`
Expected: 2.x.x printed

- [ ] **Step 4: Commit**

```
git add pyproject.toml
git commit -m "deps: add pydantic for settings validation"
```

---

### Task 2: Create `src/core/settings.py` — schema and path resolution

**Files:**
- Create: `src/core/settings.py`
- Create: `tests/test_settings.py`

- [ ] **Step 1: Write test for `get_settings_paths()`**

```python
# tests/test_settings.py
import os
import pytest


@pytest.fixture
def tmp_home(monkeypatch, tmp_path):
    """Override HOME so settings paths resolve to temp dir."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestGetSettingsPaths:
    def test_returns_user_and_project_paths(self, tmp_home, monkeypatch, tmp_path):
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        from src.core.settings import get_settings_paths
        user_path, project_path = get_settings_paths()

        assert user_path == os.path.join(str(tmp_home), ".my-agent", "settings.json")
        assert project_path == os.path.join(str(project_dir), ".my-agent", "settings.json")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_settings.py::TestGetSettingsPaths -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.core.settings'`

- [ ] **Step 3: Implement schema and path resolution**

```python
# src/core/settings.py
"""Settings file discovery, reading, merging, and validation.

Two-layer settings: user-level (~/.my-agent/settings.json) merged with
project-level ({cwd}/.my-agent/settings.json). All fields optional —
missing fields stay None and config.py fills in defaults.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


class SettingsSchema(BaseModel, extra="ignore"):
    """All fields optional. None means 'use code default'."""
    provider: Optional[str] = None
    model: Optional[str] = None
    compact_model: Optional[str] = None
    side_query_model: Optional[str] = None
    fallback_model: Optional[str] = None
    max_tokens: Optional[int] = None
    max_context_window: Optional[int] = None
    max_turns: Optional[int] = None
    max_agent_depth: Optional[int] = None
    thinking_enabled: Optional[bool] = None
    thinking_budget_tokens: Optional[int] = None
    memory_enabled: Optional[bool] = None
    memory_max_files: Optional[int] = None
    memory_max_relevant: Optional[int] = None
    session_persist_enabled: Optional[bool] = None
    micro_compact_enabled: Optional[bool] = None
    micro_compact_keep_recent: Optional[int] = None
    auto_compact_max_tokens: Optional[int] = None
    sandbox_enabled: Optional[bool] = None
    sandbox_allow_write: Optional[list[str]] = None
    sandbox_deny_write: Optional[list[str]] = None
    sandbox_deny_read: Optional[list[str]] = None
    sandbox_excluded_commands: Optional[list[str]] = None


def get_settings_paths() -> tuple[str, str]:
    """Return (user_settings_path, project_settings_path)."""
    home = str(Path.home())
    cwd = os.getcwd()
    user_path = os.path.join(home, ".my-agent", "settings.json")
    project_path = os.path.join(cwd, ".my-agent", "settings.json")
    return user_path, project_path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_settings.py::TestGetSettingsPaths -v`
Expected: PASS

- [ ] **Step 5: Commit**

```
git add src/core/settings.py tests/test_settings.py
git commit -m "feat(settings): add SettingsSchema and path resolution"
```

---

### Task 3: Implement settings file reading and merging

**Files:**
- Modify: `src/core/settings.py`
- Modify: `tests/test_settings.py`

- [ ] **Step 1: Write tests for `_read_settings_file()` and `load_settings()`**

```python
# append to tests/test_settings.py
import json


class TestReadSettingsFile:
    def test_reads_valid_json(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"model": "claude-opus-4-6", "max_tokens": 8192}))

        from src.core.settings import _read_settings_file
        result = _read_settings_file(str(f))
        assert result.model == "claude-opus-4-6"
        assert result.max_tokens == 8192
        assert result.thinking_enabled is None  # not set

    def test_returns_empty_for_missing_file(self, tmp_path):
        from src.core.settings import _read_settings_file
        result = _read_settings_file(str(tmp_path / "nope.json"))
        assert result.model is None
        assert result.max_tokens is None

    def test_ignores_unknown_fields(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"model": "x", "unknown_field": 42}))

        from src.core.settings import _read_settings_file
        result = _read_settings_file(str(f))
        assert result.model == "x"

    def test_warns_on_invalid_type(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"max_tokens": "not_a_number"}))

        from src.core.settings import _read_settings_file
        result = _read_settings_file(str(f))
        # Validation failed → returns empty schema
        assert result.max_tokens is None

    def test_handles_malformed_json(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text("{broken json")

        from src.core.settings import _read_settings_file
        result = _read_settings_file(str(f))
        assert result.model is None


class TestLoadSettings:
    def test_merges_user_and_project(self, tmp_home, monkeypatch, tmp_path):
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        # User-level settings
        user_dir = tmp_home / ".my-agent"
        user_dir.mkdir(parents=True)
        (user_dir / "settings.json").write_text(json.dumps({
            "model": "user-model",
            "max_tokens": 4096,
            "thinking_enabled": True,
        }))

        # Project-level settings (overrides model only)
        proj_dir = project_dir / ".my-agent"
        proj_dir.mkdir()
        (proj_dir / "settings.json").write_text(json.dumps({
            "model": "project-model",
        }))

        from src.core.settings import load_settings
        s = load_settings()
        assert s.model == "project-model"     # project wins
        assert s.max_tokens == 4096            # user value kept
        assert s.thinking_enabled is True      # user value kept

    def test_user_only(self, tmp_home, monkeypatch, tmp_path):
        project_dir = tmp_path / "proj2"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        user_dir = tmp_home / ".my-agent"
        user_dir.mkdir(parents=True)
        (user_dir / "settings.json").write_text(json.dumps({
            "model": "user-model",
        }))

        from src.core.settings import load_settings
        s = load_settings()
        assert s.model == "user-model"

    def test_no_files(self, tmp_home, monkeypatch, tmp_path):
        project_dir = tmp_path / "proj3"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        from src.core.settings import load_settings
        s = load_settings()
        assert s.model is None
        assert s.max_tokens is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_settings.py::TestReadSettingsFile tests/test_settings.py::TestLoadSettings -v`
Expected: FAIL — `_read_settings_file` and `load_settings` not defined

- [ ] **Step 3: Implement `_read_settings_file()` and `load_settings()`**

Append to `src/core/settings.py`:

```python
def _read_settings_file(path: str) -> SettingsSchema:
    """Read and validate a single settings.json file.
    Returns empty SettingsSchema on any error (missing file, bad JSON, validation).
    """
    if not os.path.isfile(path):
        return SettingsSchema()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read settings file %s: %s", path, e)
        return SettingsSchema()

    if not isinstance(data, dict):
        logger.warning("Settings file %s is not a JSON object, ignoring", path)
        return SettingsSchema()

    try:
        return SettingsSchema(**data)
    except ValidationError as e:
        logger.warning("Settings validation error in %s: %s", path, e)
        return SettingsSchema()


def load_settings() -> SettingsSchema:
    """Load and merge settings: user-level → project-level override.
    Project-level non-None fields override user-level.
    """
    user_path, project_path = get_settings_paths()
    user_settings = _read_settings_file(user_path)
    proj_settings = _read_settings_file(project_path)

    merged: dict = {}
    for field_name in SettingsSchema.model_fields:
        proj_val = getattr(proj_settings, field_name)
        user_val = getattr(user_settings, field_name)
        merged[field_name] = proj_val if proj_val is not None else user_val

    return SettingsSchema(**merged)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_settings.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```
git add src/core/settings.py tests/test_settings.py
git commit -m "feat(settings): implement file reading and two-layer merge"
```

---

### Task 4: Implement `ensure_default_settings()` and `get_settings_file_paths()`

**Files:**
- Modify: `src/core/settings.py`
- Modify: `tests/test_settings.py`

- [ ] **Step 1: Write tests**

```python
# append to tests/test_settings.py

class TestEnsureDefaultSettings:
    def test_creates_file_when_missing(self, tmp_home):
        from src.core.settings import ensure_default_settings
        user_path = os.path.join(str(tmp_home), ".my-agent", "settings.json")
        assert not os.path.isfile(user_path)

        ensure_default_settings()

        assert os.path.isfile(user_path)
        with open(user_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "model" in data
        assert "provider" in data

    def test_does_not_overwrite_existing(self, tmp_home):
        from src.core.settings import ensure_default_settings
        user_dir = tmp_home / ".my-agent"
        user_dir.mkdir(parents=True)
        user_path = user_dir / "settings.json"
        user_path.write_text(json.dumps({"model": "my-custom"}))

        ensure_default_settings()

        with open(str(user_path), "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["model"] == "my-custom"


class TestGetSettingsFilePaths:
    def test_returns_both_paths(self, tmp_home, monkeypatch, tmp_path):
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        from src.core.settings import get_settings_file_paths
        paths = get_settings_file_paths()
        assert len(paths) == 2
        assert all(p.endswith("settings.json") for p in paths)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_settings.py::TestEnsureDefaultSettings tests/test_settings.py::TestGetSettingsFilePaths -v`
Expected: FAIL — functions not defined

- [ ] **Step 3: Implement**

Append to `src/core/settings.py`:

```python
_DEFAULT_SETTINGS = {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "thinking_enabled": True,
    "thinking_budget_tokens": 10000,
    "memory_enabled": True,
}


def ensure_default_settings() -> None:
    """Create ~/.my-agent/settings.json with defaults if it doesn't exist."""
    user_path, _ = get_settings_paths()
    if os.path.isfile(user_path):
        return
    os.makedirs(os.path.dirname(user_path), exist_ok=True)
    with open(user_path, "w", encoding="utf-8") as f:
        json.dump(_DEFAULT_SETTINGS, f, indent=2, ensure_ascii=False)
        f.write("\n")
    logger.info("Created default settings: %s", user_path)


def get_settings_file_paths() -> list[str]:
    """Return all settings file absolute paths (for watcher registration)."""
    user_path, project_path = get_settings_paths()
    return [user_path, project_path]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_settings.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```
git add src/core/settings.py tests/test_settings.py
git commit -m "feat(settings): add ensure_default_settings and path listing"
```

---

### Task 5: Refactor `config.py` to load from settings

**Files:**
- Modify: `src/core/config.py`
- Modify: `tests/test_settings.py`

- [ ] **Step 1: Write test for settings → config integration**

```python
# append to tests/test_settings.py

class TestConfigReload:
    def test_reload_picks_up_settings(self, tmp_home, monkeypatch, tmp_path):
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        # Write user settings
        user_dir = tmp_home / ".my-agent"
        user_dir.mkdir(parents=True)
        (user_dir / "settings.json").write_text(json.dumps({
            "max_tokens": 9999,
            "thinking_enabled": False,
            "memory_max_relevant": 10,
        }))

        from src.core import config
        config.reload()

        assert config.MAX_TOKENS == 9999
        assert config.THINKING_ENABLED is False
        assert config.MEMORY_MAX_RELEVANT == 10

    def test_env_var_overrides_settings(self, tmp_home, monkeypatch, tmp_path):
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        user_dir = tmp_home / ".my-agent"
        user_dir.mkdir(parents=True)
        (user_dir / "settings.json").write_text(json.dumps({
            "provider": "deepseek",
        }))

        # Env var takes priority
        monkeypatch.setenv("AGENT_PROVIDER", "anthropic")
        from src.core import config
        config.reload()

        assert config.PROVIDER == "anthropic"

    def test_reload_without_settings_keeps_defaults(self, tmp_home, monkeypatch, tmp_path):
        project_dir = tmp_path / "empty_proj"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        from src.core import config
        config.reload()

        # Should still have sensible defaults
        assert config.MAX_TOKENS == 16384
        assert config.THINKING_ENABLED is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_settings.py::TestConfigReload -v`
Expected: FAIL — `config.reload` not defined

- [ ] **Step 3: Refactor config.py**

Replace `src/core/config.py` with the following structure. Key changes:
- All defaults defined as a `_DEFAULTS` dict
- `_apply_settings()` helper merges settings + env overrides
- Module-level variables initialized via `_apply_settings()`
- `reload()` re-reads settings and refreshes all module-level variables

```python
"""Configuration — loaded from settings.json + environment variables.

Priority (low → high): code defaults → user settings.json → project settings.json → env vars.
"""

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Session identity — unique per process lifetime, not configurable via settings
# ---------------------------------------------------------------------------
SESSION_ID: str = os.environ.get("AGENT_SESSION_ID", uuid.uuid4().hex[:12])

# ---------------------------------------------------------------------------
# API credentials — env-only, never in settings.json
# ---------------------------------------------------------------------------
ANTHROPIC_AUTH_TOKEN: str = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
ANTHROPIC_BASE_URL: str | None = os.environ.get("ANTHROPIC_BASE_URL", None)
DEEPSEEK_API_KEY: str = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str | None = os.environ.get("DEEPSEEK_BASE_URL", None)
DEEPSEEK_REASONER_MODEL: str = os.environ.get("DEEPSEEK_REASONER_MODEL", "deepseek-reasoner")

# ---------------------------------------------------------------------------
# Code-only constants — not user-configurable
# ---------------------------------------------------------------------------
PROMPT_CACHE_ENABLED: bool = True
PROMPT_CACHE_TTL_MINUTES: int = 5
BYTES_PER_TOKEN: int = 2
DISABLE_STREAMING_FALLBACK: bool = os.environ.get(
    "DISABLE_STREAMING_FALLBACK", ""
).lower() in ("1", "true")

# ---------------------------------------------------------------------------
# Model tiers
# ---------------------------------------------------------------------------

@dataclass
class ProviderModels:
    provider: str
    main: str
    compact: str
    side_query: str
    fallback: str


def _build_models(provider: str, settings) -> ProviderModels:
    """Build ProviderModels: provider defaults → settings → env vars."""
    from src.providers import get_provider

    adapter = get_provider(provider)
    defaults = adapter.get_default_models()

    s_main = settings.model
    s_compact = settings.compact_model
    s_side = settings.side_query_model
    s_fallback = settings.fallback_model

    env_main = os.environ.get("AGENT_MODEL")
    main = env_main or s_main or defaults.main
    compact = (os.environ.get("AGENT_COMPACT_MODEL")
               or s_compact
               or (main if (env_main or s_main) else defaults.compact))
    side_query = os.environ.get("AGENT_SIDE_QUERY_MODEL") or s_side or defaults.side_query
    fallback = os.environ.get("AGENT_FALLBACK_MODEL") or s_fallback or defaults.fallback

    return ProviderModels(
        provider=provider,
        main=main, compact=compact,
        side_query=side_query, fallback=fallback,
    )


# ---------------------------------------------------------------------------
# Configurable variables — initialized by _apply_settings(), refreshed by reload()
# ---------------------------------------------------------------------------
PROVIDER: str = ""
MODELS: ProviderModels = None  # type: ignore[assignment]
MODEL: str = ""
MAX_TOKENS: int = 0
MAX_CONTEXT_WINDOW: int = 0
MAX_TURNS: int = 0
MAX_AGENT_DEPTH: int = 0
THINKING_ENABLED: bool = True
THINKING_BUDGET_TOKENS: int = 0
MEMORY_ENABLED: bool = True
MEMORY_MAX_FILES: int = 0
MEMORY_MAX_RELEVANT: int = 0
SESSION_PERSIST_ENABLED: bool = True
MICRO_COMPACT_ENABLED: bool = True
MICRO_COMPACT_KEEP_RECENT: int = 0
AUTO_COMPACT_MAX_TOKENS: int = 0
SANDBOX_ENABLED: bool = True
SANDBOX_ALLOW_WRITE: list[str] = []
SANDBOX_DENY_WRITE: list[str] = []
SANDBOX_DENY_READ: list[str] = []
SANDBOX_EXCLUDED_COMMANDS: list[str] = []


def _apply_settings() -> None:
    """Read settings.json, merge with defaults + env vars, set module globals."""
    global PROVIDER, MODELS, MODEL
    global MAX_TOKENS, MAX_CONTEXT_WINDOW, MAX_TURNS, MAX_AGENT_DEPTH
    global THINKING_ENABLED, THINKING_BUDGET_TOKENS
    global MEMORY_ENABLED, MEMORY_MAX_FILES, MEMORY_MAX_RELEVANT
    global SESSION_PERSIST_ENABLED
    global MICRO_COMPACT_ENABLED, MICRO_COMPACT_KEEP_RECENT
    global AUTO_COMPACT_MAX_TOKENS
    global SANDBOX_ENABLED, SANDBOX_ALLOW_WRITE, SANDBOX_DENY_WRITE
    global SANDBOX_DENY_READ, SANDBOX_EXCLUDED_COMMANDS

    from src.core.settings import load_settings
    s = load_settings()

    def _val(settings_val, default, env_key=None, cast=None):
        """Resolve: env var > settings > default."""
        if env_key:
            env = os.environ.get(env_key)
            if env is not None:
                return cast(env) if cast else env
        return settings_val if settings_val is not None else default

    PROVIDER = _val(s.provider, "anthropic", "AGENT_PROVIDER")
    MAX_TOKENS = _val(s.max_tokens, 16384)
    MAX_CONTEXT_WINDOW = _val(s.max_context_window, 200_000)
    MAX_TURNS = _val(s.max_turns, 30)
    MAX_AGENT_DEPTH = _val(s.max_agent_depth, 3)
    THINKING_ENABLED = _val(s.thinking_enabled, True)
    THINKING_BUDGET_TOKENS = _val(s.thinking_budget_tokens, 10000)
    MEMORY_ENABLED = _val(s.memory_enabled, True)
    MEMORY_MAX_FILES = _val(s.memory_max_files, 200)
    MEMORY_MAX_RELEVANT = _val(s.memory_max_relevant, 5)
    SESSION_PERSIST_ENABLED = _val(
        s.session_persist_enabled, True, "AGENT_SESSION_PERSIST",
        cast=lambda v: v != "0",
    )
    MICRO_COMPACT_ENABLED = _val(s.micro_compact_enabled, True)
    MICRO_COMPACT_KEEP_RECENT = _val(s.micro_compact_keep_recent, 6)
    AUTO_COMPACT_MAX_TOKENS = _val(s.auto_compact_max_tokens, 4096)
    SANDBOX_ENABLED = _val(s.sandbox_enabled, True)
    SANDBOX_ALLOW_WRITE = s.sandbox_allow_write if s.sandbox_allow_write is not None else []
    SANDBOX_DENY_WRITE = s.sandbox_deny_write if s.sandbox_deny_write is not None else []
    SANDBOX_DENY_READ = s.sandbox_deny_read if s.sandbox_deny_read is not None else []
    SANDBOX_EXCLUDED_COMMANDS = s.sandbox_excluded_commands if s.sandbox_excluded_commands is not None else []

    MODELS = _build_models(PROVIDER, s)
    MODEL = MODELS.main


# Run on module import
_apply_settings()


def reload() -> None:
    """Re-read settings.json and refresh all configurable module variables.
    Called by watcher on file change.
    """
    _apply_settings()

    # Invalidate sandbox_manager cached config so it picks up new values
    try:
        from src.sandbox import sandbox_manager
        sandbox_manager._config = None
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Token estimation — code-only constants, not configurable
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    return len(text.encode("utf-8")) // BYTES_PER_TOKEN


def tokens_to_chars(tokens: int) -> int:
    return tokens * BYTES_PER_TOKEN


# ---------------------------------------------------------------------------
# Directory paths — unchanged
# ---------------------------------------------------------------------------

def get_skill_dirs() -> list[str]:
    cwd = os.getcwd()
    home = str(Path.home())
    dirs = [
        os.path.join(cwd, "skills"),
        os.path.join(cwd, ".claude", "skills"),
        os.path.join(home, ".claude", "skills"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    return dirs


def get_agent_dirs() -> list[str]:
    cwd = os.getcwd()
    dirs = [
        os.path.join(cwd, "agents"),
        os.path.join(cwd, ".claude", "agents"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    return dirs


def get_permission_config_paths() -> tuple[str, str]:
    home = str(Path.home())
    cwd = os.getcwd()
    user_config = os.path.join(home, ".my-agent", "permissions.json")
    project_config = os.path.join(cwd, "agent-permissions.json")
    return user_config, project_config
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_settings.py -v`
Expected: All PASS

- [ ] **Step 5: Run existing tests to check for regressions**

Run: `python -m pytest tests/ -v --ignore=tests/demos`
Expected: No new failures (existing tests pass)

- [ ] **Step 6: Commit**

```
git add src/core/config.py tests/test_settings.py
git commit -m "feat(settings): refactor config.py to load from settings.json + reload()"
```

---

### Task 6: Add settings watcher to `watcher.py`

**Files:**
- Modify: `src/watcher.py:17-19` (add constant)
- Modify: `src/watcher.py:134-238` (add watcher in `start_watchers()`)

- [ ] **Step 1: Write test for settings reload callback**

```python
# append to tests/test_settings.py

class TestSettingsWatcher:
    def test_reload_settings_function_exists(self):
        """Verify the watcher callback is importable and callable."""
        from src.watcher import _reload_settings
        assert callable(_reload_settings)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_settings.py::TestSettingsWatcher -v`
Expected: FAIL — `_reload_settings` not importable

- [ ] **Step 3: Add settings watcher to `watcher.py`**

Add constant at line 20 (after `PERMISSION_DEBOUNCE`):

```python
SETTINGS_DEBOUNCE = 0.5
```

Add reload function after `_reload_permissions()`:

```python
def _reload_settings() -> None:
    from src.core import config
    config.reload()
    log.info("[watcher] Settings reloaded.")
```

Add watcher registration in `start_watchers()`, after the permission watcher block (before `try: observer.start()`):

```python
    # Settings file watcher
    from src.core.settings import get_settings_file_paths
    settings_notifier = _DebouncedNotifier(
        loop, _reload_settings, SETTINGS_DEBOUNCE)
    settings_paths = get_settings_file_paths()
    if settings_paths:
        settings_handler = _McpConfigHandler(
            {os.path.basename(p) for p in settings_paths},
            settings_notifier,
        )
        watched_settings_dirs: set[str] = set()
        for p in settings_paths:
            parent = os.path.dirname(p)
            if parent not in watched_settings_dirs:
                os.makedirs(parent, exist_ok=True)
                observer.schedule(settings_handler, parent, recursive=False)
                watched_settings_dirs.add(parent)
                log.info(f"Watching settings: {p}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_settings.py::TestSettingsWatcher -v`
Expected: PASS

- [ ] **Step 5: Commit**

```
git add src/watcher.py tests/test_settings.py
git commit -m "feat(settings): add watchdog hot-reload for settings.json"
```

---

### Task 7: Call `ensure_default_settings()` on startup

**Files:**
- Modify: `src/main.py:46-47`

- [ ] **Step 1: Add ensure_default_settings call in `async_main()`**

In `src/main.py`, after line 47 (`ensure_memory_dir()`), add:

```python
    from src.core.settings import ensure_default_settings
    ensure_default_settings()
```

- [ ] **Step 2: Verify startup works**

Run: `python -m src.main` (then type `exit`)
Expected: Starts without errors. `~/.my-agent/settings.json` exists.

- [ ] **Step 3: Commit**

```
git add src/main.py
git commit -m "feat(settings): create default settings.json on first startup"
```

---

### Task 8: Integration test — end-to-end settings flow

**Files:**
- Modify: `tests/test_settings.py`

- [ ] **Step 1: Write integration test**

```python
# append to tests/test_settings.py

class TestIntegration:
    def test_full_flow(self, tmp_home, monkeypatch, tmp_path):
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
        assert config.MODEL == "claude-sonnet-4-6"

        # 3. Override with project settings
        proj_settings_dir = project_dir / ".my-agent"
        proj_settings_dir.mkdir()
        (proj_settings_dir / "settings.json").write_text(json.dumps({
            "max_turns": 50,
            "thinking_enabled": False,
        }))

        # 4. Reload picks up project override
        config.reload()
        assert config.MAX_TURNS == 50
        assert config.THINKING_ENABLED is False
        # User-level model still preserved
        assert config.MODEL == "claude-sonnet-4-6"
```

- [ ] **Step 2: Run all settings tests**

Run: `python -m pytest tests/test_settings.py -v`
Expected: All PASS

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -v --ignore=tests/demos`
Expected: No regressions

- [ ] **Step 4: Commit**

```
git add tests/test_settings.py
git commit -m "test(settings): add integration test for full settings flow"
```

---

## Self-Review Checklist

- **Spec coverage:**
  - ✅ SettingsSchema with pydantic validation
  - ✅ File path resolution (user + project)
  - ✅ Two-layer merge (user → project override)
  - ✅ `config.reload()` with env-var priority
  - ✅ Watcher hot-reload via `_DebouncedNotifier`
  - ✅ `ensure_default_settings()` for first-run
  - ✅ Sandbox manager cache invalidation on reload
  - ✅ Model priority: provider default → settings → env var

- **Placeholder scan:** No TBDs, TODOs, or "implement later"

- **Type consistency:** `SettingsSchema` used consistently; `load_settings()` returns `SettingsSchema`; `_read_settings_file()` returns `SettingsSchema`; `_build_models()` accepts settings object; `_val()` helper used consistently for env-var override pattern
