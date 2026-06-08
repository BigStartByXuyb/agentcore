"""MCP server configuration loading.

Reads MCP server definitions from JSON config files and returns
structured ServerConfig objects.

Config file format (.mcp.json):
{
  "mcpServers": {
    "<server-name>": {
      "command": "<executable>",
      "args": ["<arg1>", "<arg2>"],
      "env": {"KEY": "value"}           // optional
    }
  }
}

Search order (later overrides earlier):
  1. Global:  ~/.my-agent/mcp.json
  2. Project: <cwd>/.mcp.json
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class McpServerConfig:
    """A single MCP server definition."""
    name: str
    type: str
    url: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    scope: str = "project"  # "global" | "project"


def config_hash(cfg: McpServerConfig) -> str:
    """SHA-256 前 16 位 hex，排除 scope。"""
    blob = json.dumps({
        "name": cfg.name, "type": cfg.type, "url": cfg.url,
        "command": cfg.command, "args": cfg.args, "env": cfg.env,
    }, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def get_mcp_config_paths(
    *,
    project_dir: str | Path | None = None,
    global_dir: str | Path | None = None,
) -> list[tuple[Path, str]]:
    """Return MCP config file paths with scope (may or may not exist on disk).

    Returns [(path, scope), ...] — order is global first, project second.
    """
    if global_dir is None:
        global_dir = Path.home() / ".my-agent"
    else:
        global_dir = Path(global_dir)
    if project_dir is None:
        project_dir = Path.cwd()
    else:
        project_dir = Path(project_dir)
    return [
        (global_dir / "mcp.json", "global"),
        (project_dir / ".mcp.json", "project"),
    ]


def _expand_env_vars(env: dict[str, str]) -> dict[str, str]:
    """Expand ${VAR} references in env values using os.environ."""
    result = {}
    for key, value in env.items():
        result[key] = os.path.expandvars(value)
    return result


def _parse_mcp_file(path: Path, scope: str) -> list[McpServerConfig]:
    """Parse a single .mcp.json file into ServerConfig list."""
    if not path.is_file():
        return []

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to parse MCP config %s: %s", path, e)
        return []

    servers_block = data.get("mcpServers", {})
    if not isinstance(servers_block, dict):
        logger.warning("Invalid mcpServers in %s: expected object", path)
        return []

    configs: list[McpServerConfig] = []
    for name, server_def in servers_block.items():
        if not isinstance(server_def, dict):
            logger.warning("Skipping invalid server '%s' in %s", name, path)
            continue

        command = server_def.get("command")
        if not isinstance(command, str):
            command = ""

        args = server_def.get("args", [])
        if not isinstance(args, list):
            args = []

        env = server_def.get("env", {})
        if not isinstance(env, dict):
            env = {}

        connect_type = server_def.get("type", "")
        if not isinstance(connect_type, str):
            connect_type = ""

        url = server_def.get("url", "")
        if not isinstance(url, str):
            url = ""

        configs.append(McpServerConfig(
            name=name,
            type=connect_type,
            url=url,
            command=command,
            args=[str(a) for a in args],
            env=_expand_env_vars(env),
            scope=scope,
        ))

    return configs


def load_mcp_configs(
    *,
    project_dir: str | Path | None = None,
    global_dir: str | Path | None = None,
) -> list[McpServerConfig]:
    """Load MCP server configs from global + project files.

    Search order (later overrides earlier by server name):
      1. Global:  <global_dir>/mcp.json   (default: ~/.my-agent/)
      2. Project: <project_dir>/.mcp.json (default: cwd)

    Returns deduplicated list — project configs override global
    configs with the same server name.
    """
    config_paths = get_mcp_config_paths(
        project_dir=project_dir, global_dir=global_dir)

    # Deduplicate: later scopes override earlier (project > global)
    by_name: dict[str, McpServerConfig] = {}
    for path, scope in config_paths:
        for cfg in _parse_mcp_file(path, scope=scope):
            by_name[cfg.name] = cfg

    result = list(by_name.values())
    logger.info("Loaded %d MCP server config(s) from %d sources",
                len(result), len(config_paths))
    return result
