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
# ---------------------------------------------------------------------------
# McpClient — sync wrapper around the async MCP SDK Session
# ---------------------------------------------------------------------------

class McpClient:
    """Synchronous wrapper owning one MCP server connection.

    MCP SDK's ClientSession is an async context manager and cannot be
    stored as a plain field. This class runs an asyncio event loop on a
    background daemon thread and forwards sync methods to the async
    Session via asyncio.run_coroutine_threadsafe.

    One instance per MCP server (servers are independent subprocesses).
    """

    def __init__(self, cfg: McpServerConfig):
        self._cfg: McpServerConfig = cfg
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any | None = None  # holds mcp.ClientSession once connected
        self._ready_event: threading.Event | None = None   # 主线程等"连接完成"的跨线程信号
        self._close_event: asyncio.Event | None = None     # loop 内等"该关了"的 asyncio 信号
        self._lifecycle_fut: Any | None = None             # 长活 task 的 concurrent.futures.Future
        self._connect()

    async def get_tool_list_by_mcp(self) -> dict[str, list[dict[str, Any]]]:
        """MCP tools/list — return this server's tool catalog."""
        if self._session is None:
            raise RuntimeError("Session not initialized")
        result = await self._session.list_tools()
        tool_list: list[dict[str, Any]] = []
        for tool in result.tools:
            tool_list.append({
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            })
        return {self._cfg.name: tool_list}

    def list_tools(self) -> dict[str, list[dict[str, Any]]]:
        """MCP tools/list — return this server's tool catalog."""
        if self._loop is None:
            raise RuntimeError("Loop not started")
        fut = asyncio.run_coroutine_threadsafe(self.get_tool_list_by_mcp(), self._loop)
        return fut.result(timeout=30)

    async def call_tool_async(self, name: str, arguments: dict) -> Any:
        """MCP tools/call — invoke a tool on this server."""
        if self._session is None:
            raise RuntimeError("Session not initialized")
        result = await self._session.call_tool(name, arguments)
        return result

    def call_tool(self, name: str, arguments: dict) -> Any:
        """MCP tools/call — invoke a tool on this server."""
        if self._loop is None:
            raise RuntimeError("Loop not started")
        fut = asyncio.run_coroutine_threadsafe(self.call_tool_async(name, arguments), self._loop)
        return fut.result(timeout=60)

    def close(self) -> None:
        """Stop the background loop and close the Session.

        关闭顺序：
        1. set close_event → 后台 task 醒来，退出 async with → aclose 在同一 task 里跑
        2. 等 lifecycle task 跑完（aclose 完成）
        3. 停 loop、回收线程
        """
        # 1. 通知后台长活 task "该关了"
        if self._loop is not None and self._close_event is not None:
            self._loop.call_soon_threadsafe(self._close_event.set)

        # 2. 等长活 task 跑完 aclose
        if self._lifecycle_fut is not None:
            try:
                self._lifecycle_fut.result(timeout=10)
            except Exception:
                pass  # 关闭期异常吞掉，保证后面 stop/join 一定执行

        # 3. 停 loop + 回收线程
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

        self._session = None
        self._close_event = None
        self._lifecycle_fut = None
        self._loop = None
        self._thread = None

    def _connect(self) -> None:
        """Start background loop and schedule the lifecycle task.

        阻塞直到 _async_lifecycle 跑到 initialize 完成、set 了 ready_event。
        """
        # 1. 起后台 loop + 线程
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._thread.start()

        # 2. 跨线程 ready 信号（threading.Event — 主线程能 wait）
        self._ready_event = threading.Event()

        # 3. 把长活 task 扔到后台 loop —— 注意不等它结束，它会一直挂到 close()
        self._lifecycle_fut = asyncio.run_coroutine_threadsafe(
            self._async_lifecycle(), self._loop
        )

        # 4. 主线程阻塞在这里，直到后台 task initialize 完成并 set ready_event
        if not self._ready_event.wait(timeout=30):
            # 超时 — 后台可能卡在 initialize 或者 subprocess 根本没起来
            raise RuntimeError(
                f"MCP server {self._cfg.name!r} failed to initialize within 30s"
            )
        # 如果 lifecycle task 早早就异常退出（比如 spawn 失败），也把异常冒出来
        if self._lifecycle_fut.done():
            exc = self._lifecycle_fut.exception()
            if exc is not None:
                raise exc

    async def _async_lifecycle(self) -> None:
        """Long-lived task: connect, stay alive, clean up on close.

        关键点：enter_async_context 和 __aexit__（async with 退出）
        都发生在这一个 task 里，满足 anyio cancel scope 的规则。
        """
        # Delayed imports — 避免和 PyPI `mcp` 包的名字冲突
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        # asyncio.Event 必须在 loop 线程内创建（它会绑定当前 loop）
        self._close_event = asyncio.Event()

        async with AsyncExitStack() as stack:
            server_params = StdioServerParameters(
                command=self._cfg.command,
                env=self._cfg.env,
                args=self._cfg.args,
            )
            read, write = await stack.enter_async_context(stdio_client(server_params))
            session: ClientSession = await stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()  # MCP 协议握手
            self._session = session

            # 告诉主线程"可以用了"——即便下面挂住，主线程也能继续
            if self._ready_event is not None:
                self._ready_event.set()

            # 挂在这里，一直到 close() 触发 close_event.set()
            await self._close_event.wait()
        # async with 在这里退出 → stack.__aexit__ → ClientSession + stdio_client
        # 依次 aclose，全部在同一个 task 里执行，不踩 anyio cancel scope 错误


# ---------------------------------------------------------------------------
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
    client: McpClient
    tool_names: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Module-level registry of live servers
# ---------------------------------------------------------------------------

_servers: dict[str, McpServer] = {}

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
def _make_mcp_executor(tool_name:str, client: McpClient) -> callable:
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

def load_mcp_tools() -> list[ToolDef]:
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
    result = load_mcp_configs()
    tool_defs: list[ToolDef] = []
    for cfg in result:
        try:
            client = McpClient(cfg)
            tool_list = client.list_tools()
            tool_names = []
            for server_name, tools in tool_list.items():
                for tool in tools:
                    qualified_name = f"mcp__{server_name}__{tool['name']}"
                    tool_def = ToolDef( 
                        schema= _make_mcp_schema(qualified_name,tool),
                        executor=_make_mcp_executor(tool['name'], client),
                        map_result=_map_result,
                    )
                    tool_defs.append(tool_def)
                    tool_names.append(qualified_name)
            _servers[cfg.name] = McpServer(config=cfg, client=client, tool_names=tool_names)
        except Exception as e:
            print(f"Failed to connect to MCP server {cfg.name!r}: {e}")
    return tool_defs

def register_mcp_tools(all_tools: dict[str, ToolDef]) -> None:
    """Append MCP ToolDefs into the given ALL_TOOLS registry."""
    for td in load_mcp_tools():
        all_tools[td.name] = td


def shutdown_mcp() -> None:
    """Close all MCP connections — call on process exit."""
    for server in _servers.values():
        server.client.close()
    _servers.clear()
