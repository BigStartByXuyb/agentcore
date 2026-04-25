"""Sandbox manager — OS-level isolation for bash commands via bubblewrap.

Provides filesystem read/write restrictions enforced at the kernel level.
Only supported on Linux and WSL2+. On unsupported platforms (Windows native,
WSL1), silently degrades to no sandbox — matching Claude Code's behavior.

Usage:
    from src.sandbox import sandbox_manager
    if sandbox_manager.is_enabled():
        cmd = sandbox_manager.wrap_command(["bash", "-c", "npm install"])
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field

from src import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def _detect_platform() -> str:
    """Detect runtime platform. Returns 'linux', 'wsl2', 'wsl1', 'macos', 'windows'."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "linux":
        try:
            with open("/proc/version", "r") as f:
                version = f.read().lower()
            if "microsoft" in version or "wsl" in version:
                if "wsl2" in version:
                    return "wsl2"
                return "wsl1"
        except OSError:
            pass
        return "linux"
    return "unknown"


_SUPPORTED_PLATFORMS = {"linux", "wsl2"}


# ---------------------------------------------------------------------------
# Sandbox config
# ---------------------------------------------------------------------------

@dataclass
class SandboxConfig:
    enabled: bool = True
    allow_write: list[str] = field(default_factory=list)
    deny_write: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    excluded_commands: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Sandbox manager
# ---------------------------------------------------------------------------

class SandboxManager:
    """Manages bubblewrap-based command sandboxing."""

    def __init__(self) -> None:
        self._platform = _detect_platform()
        self._bwrap_path: str | None = shutil.which("bwrap")
        self._config: SandboxConfig | None = None

    def _get_config(self) -> SandboxConfig:
        if self._config is None:
            self._config = SandboxConfig(
                enabled=config.SANDBOX_ENABLED,
                allow_write=list(config.SANDBOX_ALLOW_WRITE),
                deny_write=list(config.SANDBOX_DENY_WRITE),
                deny_read=list(config.SANDBOX_DENY_READ),
                excluded_commands=list(config.SANDBOX_EXCLUDED_COMMANDS),
            )
        return self._config

    def is_available(self) -> bool:
        """True if platform supports sandboxing and bwrap is installed."""
        return (
            self._platform in _SUPPORTED_PLATFORMS
            and self._bwrap_path is not None
        )

    def is_enabled(self) -> bool:
        """True if sandbox is both available and enabled in config."""
        return self.is_available() and self._get_config().enabled

    def get_unavailable_reason(self) -> str | None:
        """Human-readable reason why sandbox is unavailable, or None."""
        if self._platform not in _SUPPORTED_PLATFORMS:
            return (
                f"Sandbox not supported on {self._platform}. "
                f"Requires Linux or WSL2 with bubblewrap installed."
            )
        if self._bwrap_path is None:
            return (
                "bubblewrap (bwrap) not found. "
                "Install with: sudo apt install bubblewrap"
            )
        return None

    def is_command_excluded(self, command: str) -> bool:
        """Check if a command should skip sandboxing."""
        cfg = self._get_config()
        cmd_stripped = command.strip()
        for pattern in cfg.excluded_commands:
            if pattern.endswith("*"):
                if cmd_stripped.startswith(pattern[:-1]):
                    return True
            elif cmd_stripped == pattern:
                return True
        return False

    def wrap_command(self, cmd: list[str]) -> list[str]:
        """Wrap a shell command with bwrap for sandboxed execution.

        Args:
            cmd: Original command, e.g. ["bash", "-c", "npm install"]

        Returns:
            Wrapped command with bwrap prefix, or original cmd if
            sandbox is not enabled.
        """
        if not self.is_enabled():
            return cmd

        assert self._bwrap_path is not None
        cfg = self._get_config()

        args: list[str] = [self._bwrap_path]

        # Mount entire filesystem read-only
        args.extend(["--ro-bind", "/", "/"])

        # Essential virtual filesystems
        args.extend(["--dev", "/dev"])
        args.extend(["--proc", "/proc"])

        # Writable: /tmp always needed
        args.extend(["--tmpfs", "/tmp"])

        # Writable: project directory (cwd)
        cwd = os.getcwd()
        args.extend(["--bind", cwd, cwd])

        # Writable: system temp dir (if different from /tmp)
        sys_tmp = tempfile.gettempdir()
        if os.path.realpath(sys_tmp) != os.path.realpath("/tmp"):
            args.extend(["--bind", sys_tmp, sys_tmp])

        # Writable: user-configured additional paths
        for path in cfg.allow_write:
            resolved = os.path.realpath(os.path.expanduser(path))
            if os.path.exists(resolved):
                args.extend(["--bind", resolved, resolved])

        # Deny read: overlay with tmpfs to hide contents
        for path in cfg.deny_read:
            resolved = os.path.realpath(os.path.expanduser(path))
            if os.path.exists(resolved):
                args.extend(["--tmpfs", resolved])

        # Deny write on specific paths: re-mount as read-only
        # (overrides any previous --bind that made them writable)
        for path in cfg.deny_write:
            resolved = os.path.realpath(os.path.expanduser(path))
            if os.path.exists(resolved):
                args.extend(["--ro-bind", resolved, resolved])

        # Unshare network namespace (no network access inside sandbox)
        args.append("--unshare-net")

        # Separator + original command
        args.append("--")
        args.extend(cmd)

        return args

    def status_summary(self) -> str:
        """One-line status for startup display."""
        if self.is_enabled():
            return f"Sandbox: enabled (bwrap on {self._platform})"
        reason = self.get_unavailable_reason()
        if reason:
            return f"Sandbox: unavailable — {reason}"
        if not self._get_config().enabled:
            return "Sandbox: disabled by config"
        return "Sandbox: off"


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

sandbox_manager = SandboxManager()
