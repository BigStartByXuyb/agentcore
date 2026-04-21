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
import threading
from dataclasses import dataclass, field
from src.mcp_tool.config import McpServerConfig, load_mcp_configs
from src.types import ToolDef, ToolResult
from typing import Any
from src.mcp_tool.McpHttpClient import McpHttpClient
from src.mcp_tool.McpPipelineClient import McpPipelineClient
from src.mcp_tool.base import McpClientBase as McpClientBase
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
def _make_mcp_executor(tool_name:str, client: McpClientBase) -> callable:
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

def load_mcp_tools() -> tuple[list[ToolDef], list[str]]:
    """Connect all configured servers and wrap their tools as ToolDef.

    For each server in load_mcp_configs():
      1. Build McpClient (spawns background loop + opens Session)
      2. client.list_tools() → discover remote tools
      3. Wrap each as ToolDef (name: mcp__<server>__<tool>,
         executor closes over the client)
      4. Record McpServer into _servers

    Individual server connection failures are logged and skipped — they
    do not abort the whole load.
    """
    configs = load_mcp_configs()
    tool_defs: list[ToolDef] = []
    err_msgs: list[str] = []
    loop = _ensure_loop()

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

    # --- Pass 2: 统一等待就绪，注册工具 ---
    for cfg, client in pending:
        try:
            client.wait_ready()
            tool_list = client.list_tools()
            tool_names = []
            for server_name, tools in tool_list.items():
                for tool in tools:
                    qualified_name = f"mcp__{server_name}__{tool['name']}"
                    tool_def = ToolDef(
                        schema=_make_mcp_schema(qualified_name, tool),
                        executor=_make_mcp_executor(tool['name'], client),
                        map_result=_map_result,
                    )
                    tool_defs.append(tool_def)
                    tool_names.append(qualified_name)
            _servers[cfg.name] = McpServer(config=cfg, client=client, tool_names=tool_names)
        except Exception as e:
            err_msgs.append(f"Failed to connect to MCP server {cfg.name!r}: {e}")
            
    return tool_defs, err_msgs

def register_mcp_tools(tool_registry) -> None:
    """Append MCP ToolDefs into the given ToolRegistry."""
    tool_defs, _ = load_mcp_tools()
    for td in tool_defs:
        source = "mcp"
        for srv_name, srv in _servers.items():
            if td.name in srv.tool_names:
                source = f"mcp:{srv_name}"
                break
        tool_registry.register(td.name, td, source=source)



def shutdown_mcp() -> None:
    """Close all MCP connections — call on process exit."""
    for server in _servers.values():
        server.client.close()
    _servers.clear()
