"""MCP server configuration loading.

Reads MCP server definitions from JSON config files and returns
structured ServerConfig objects. Mirrors Claude Code's
src/services/mcp/config.ts config loading logic.

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
    if global_dir is None:
        global_dir = Path.home() / ".my-agent"
    else:
        global_dir = Path(global_dir)

    if project_dir is None:
        project_dir = Path.cwd()
    else:
        project_dir = Path(project_dir)

    # Collect from both scopes
    global_configs = _parse_mcp_file(global_dir / "mcp.json", scope="global")
    project_configs = _parse_mcp_file(project_dir / ".mcp.json", scope="project")

    # Deduplicate: project overrides global (by server name)
    by_name: dict[str, McpServerConfig] = {}

    # Later ones override earlier ones, so global goes first, then project
    for cfg in global_configs:
        by_name[cfg.name] = cfg
    for cfg in project_configs:
        by_name[cfg.name] = cfg  # override

    result = list(by_name.values())
    logger.info(
        "Loaded %d MCP server config(s) (%d global, %d project)",
        len(result), len(global_configs), len(project_configs),
    )
    return result
