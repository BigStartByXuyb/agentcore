"""MCP (Model Context Protocol) package.

Discovers configured MCP servers, wraps their remote tools as local
ToolDef entries, and registers them into ALL_TOOLS.

Key types:
  McpClient — sync wrapper around MCP SDK's async ClientSession
  McpServer — bundles one server's config + live client + registered tool names
  _servers  — module-level registry of connected servers
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from src.mcp_tool.config import McpServerConfig, load_mcp_configs, config_hash
from src.types import ToolDef, ToolResult
from typing import Any, Callable, TYPE_CHECKING
from src.mcp_tool.McpHttpClient import McpHttpClient
from src.mcp_tool.McpPipelineClient import McpPipelineClient
from src.mcp_tool.base import McpClientBase as McpClientBase

if TYPE_CHECKING:
    from src.tools import ToolRegistry

log = logging.getLogger(__name__)
#  ---------------------------------------------------------------------------
# McpServer — one configured + connected server bundle
# ---------------------------------------------------------------------------

@dataclass
class McpServer:
    """A single connected MCP server.

    Pairs the static config with the live client and the list of tool
    names this server registered into ALL_TOOLS (qualified as
    `mcp__<server>__<tool>`). tool_names lets us unregister a server's
    tools if needed.
    """
    config: McpServerConfig
    client: McpClientBase
    tool_names: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Module-level registry of live servers
# ---------------------------------------------------------------------------
_servers: dict[str, McpServer] = {}
_config_hashes: dict[str, str] = {}          # server_name -> config_hash
_tool_registry: ToolRegistry | None = None   # set by register_mcp_tools
_mcp_loop: asyncio.AbstractEventLoop | None = None
_mcp_thread: threading.Thread | None = None

def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _mcp_loop, _mcp_thread
    if _mcp_loop is None:
          _mcp_loop = asyncio.new_event_loop()
          _mcp_thread = threading.Thread(target=_mcp_loop.run_forever, daemon=True)
          _mcp_thread.start()
    return _mcp_loop
# ---------------------------------------------------------------------------
# Load / register / shutdown — skeletons, to be filled in next step
# ---------------------------------------------------------------------------
def _make_mcp_schema(qualified_name: str, tool: dict[str, Any]) -> dict:
    """Build a ToolDef.schema for a remote MCP tool."""
    return {
        "name": qualified_name,
        "description": f"{tool['description']}",
        "input_schema": 
            tool["input_schema"],

    }
def _make_mcp_executor(tool_name:str, client: McpClientBase) -> Callable:
    """Factory: returns an executor that invokes one specific remote MCP tool."""
    def _executor(inputs: dict, ctx) -> ToolResult:
        result = client.call_tool(tool_name, inputs)   # CallToolResult
        text = "\n".join(
            c.text for c in result.content if hasattr(c, "text")
        )
        return ToolResult(
            data={"content": text, "is_error": result.isError}
        )
    return _executor

def _map_result(result_data: dict) -> str:
    """Map the raw result dict from MCP into a string for LLM consumption."""
    # This is a simple example — you can customize it based on your needs
    content = result_data.get("content", "")
    is_error = result_data.get("is_error", False)
    if is_error:
        return f"Error: {content}"
    return content

def _connect_servers(
    configs: list[McpServerConfig],
) -> tuple[list[tuple[McpServerConfig, McpServer, list[ToolDef]]], list[str]]:
    """Two-pass connect: create clients → wait ready → discover tools.

    Returns (connected, errors) where each connected entry is
    (config, McpServer, tool_defs).  Callers decide how to register.
    Failed servers are logged and skipped; on wait_ready/list_tools
    failure the client is closed to avoid leaks.
    """
    loop = _ensure_loop()
    connected: list[tuple[McpServerConfig, McpServer, list[ToolDef]]] = []
    err_msgs: list[str] = []

    # --- Pass 1: 创建所有 client，提交连接，不阻塞 ---
    pending: list[tuple[McpServerConfig, McpClientBase]] = []
    for cfg in configs:
        try:
            if cfg.type == "http":
                client = McpHttpClient(cfg, loop=loop)
            elif cfg.type == "pipeline":
                client = McpPipelineClient(cfg, loop=loop)
            else:
                err_msgs.append(f"Unsupported MCP server type {cfg.type!r} for server {cfg.name!r}")
                continue
            client.start()
            pending.append((cfg, client))
        except Exception as e:
            err_msgs.append(f"Failed to create MCP client {cfg.name!r}: {e}")

    # --- Pass 2: 统一等待就绪，发现工具 ---
    for cfg, client in pending:
        try:
            client.wait_ready()
            tool_list = client.list_tools()
            tool_defs: list[ToolDef] = []
            tool_names: list[str] = []
            for server_name, tools in tool_list.items():
                for tool in tools:
                    qname = f"mcp__{server_name}__{tool['name']}"
                    td = ToolDef(
                        schema=_make_mcp_schema(qname, tool),
                        executor=_make_mcp_executor(tool['name'], client),
                        map_result=_map_result,
                    )
                    tool_defs.append(td)
                    tool_names.append(qname)
            srv = McpServer(config=cfg, client=client, tool_names=tool_names)
            connected.append((cfg, srv, tool_defs))
        except Exception as e:
            err_msgs.append(f"Failed to connect to MCP server {cfg.name!r}: {e}")
            try:
                client.close()
            except Exception:
                pass

    return connected, err_msgs


def load_mcp_tools() -> tuple[list[ToolDef], list[str]]:
    """Connect all configured servers and wrap their tools as ToolDef."""
    configs = load_mcp_configs()
    connected, err_msgs = _connect_servers(configs)

    all_tool_defs: list[ToolDef] = []
    for cfg, srv, tool_defs in connected:
        _servers[cfg.name] = srv
        all_tool_defs.extend(tool_defs)

    return all_tool_defs, err_msgs

def register_mcp_tools(tool_registry: ToolRegistry) -> None:
    """Append MCP ToolDefs into the given ToolRegistry."""
    global _tool_registry
    _tool_registry = tool_registry

    tool_defs, _ = load_mcp_tools()
    for td in tool_defs:
        source = "mcp"
        for srv_name, srv in _servers.items():
            if td.name in srv.tool_names:
                source = f"mcp:{srv_name}"
                break
        tool_registry.register(td.name, td, source=source)

    _config_hashes.clear()
    for name, srv in _servers.items():
        _config_hashes[name] = config_hash(srv.config)



def reload_mcp_servers() -> None:
    """Diff configs by hash, disconnect stale servers, connect new ones.

    Called from watcher debounce callback on the main asyncio loop.
    """
    if _tool_registry is None:
        log.warning("[mcp-reload] called before register_mcp_tools")
        return

    new_configs = load_mcp_configs()
    new_by_name: dict[str, McpServerConfig] = {c.name: c for c in new_configs}
    new_hashes: dict[str, str] = {c.name: config_hash(c) for c in new_configs}

    old_names = set(_config_hashes.keys())
    new_names = set(new_hashes.keys())

    removed = old_names - new_names
    added = new_names - old_names
    changed = {n for n in old_names & new_names
                if _config_hashes[n] != new_hashes[n]}
    stale = removed | changed
    fresh = added | changed

    if not stale and not fresh:
        log.debug("[mcp-reload] No config changes detected")
        return

    log.info(
        "[mcp-reload] removed=%s added=%s changed=%s unchanged=%d",
        removed or "{}", added or "{}", changed or "{}",
        len((old_names & new_names) - changed),
    )

    # --- Disconnect stale servers ---
    for name in stale:
        srv = _servers.pop(name, None)
        if srv is not None:
            try:
                srv.client.close()
            except Exception as e:
                log.warning("[mcp-reload] Error closing %s: %s", name, e)
        _tool_registry.unregister_by_source(f"mcp:{name}")
        _config_hashes.pop(name, None)

    # --- Connect fresh servers ---
    fresh_configs = [new_by_name[n] for n in fresh]
    connected, errs = _connect_servers(fresh_configs)
    for err in errs:
        log.error("[mcp-reload] %s", err)

    for cfg, srv, tool_defs in connected:
        for td in tool_defs:
            _tool_registry.register(td.name, td, source=f"mcp:{cfg.name}")
        _servers[cfg.name] = srv
        _config_hashes[cfg.name] = new_hashes[cfg.name]
        log.info("[mcp-reload] Connected %s (%d tools)",
                 cfg.name, len(tool_defs))

    # Sync hashes for unchanged servers (defensive)
    for name in (old_names & new_names) - changed:
        _config_hashes[name] = new_hashes[name]


def shutdown_mcp() -> None:
    """Close all MCP connections — call on process exit."""
    for server in _servers.values():
        server.client.close()
    _servers.clear()
