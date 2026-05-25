"""MCP (Model Context Protocol) package.

Discovers configured MCP servers, wraps their remote tools as local
ToolDef entries, and registers them into ALL_TOOLS.

Architecture (after async refactor):
  - Single event loop: all MCP lifecycle tasks + tool calls run on the main asyncio loop
  - Memoize cache: _connection_cache stores asyncio.Task per server name,
    ensuring only one connection is created per server (like Claude Code's memoize pattern)
  - Async executors: MCP tool executors are async, enabling true concurrent tool calls
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from src.mcp_tool.config import McpServerConfig, load_mcp_configs, config_hash
from src.types import ToolDef, ToolResult
from typing import Any, Callable, TYPE_CHECKING
from src.mcp_tool.McpHttpClient import McpHttpClient
from src.mcp_tool.McpPipelineClient import McpPipelineClient
from src.mcp_tool.base import McpClientBase as McpClientBase, _is_reconnectable_error

if TYPE_CHECKING:
    from src.tools import ToolRegistry

log = logging.getLogger(__name__)

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
    client: McpClientBase
    tool_names: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Module-level registry of live servers + memoize connection cache
# ---------------------------------------------------------------------------
_servers: dict[str, McpServer] = {}
_config_hashes: dict[str, str] = {}
_tool_registry: ToolRegistry | None = None
_connection_cache: dict[str, asyncio.Task[McpClientBase]] = {}
_server_generations: dict[str, int] = {}  # absent key = generation 0
_reload_lock: asyncio.Lock | None = None


def _get_reload_lock() -> asyncio.Lock:
    global _reload_lock
    if _reload_lock is None:
        _reload_lock = asyncio.Lock()
    return _reload_lock


# ---------------------------------------------------------------------------
# Memoized connection — core of the async refactor
# ---------------------------------------------------------------------------

async def _create_client(cfg: McpServerConfig) -> McpClientBase:
    """Create, start, and wait for a single MCP client to be ready."""
    if cfg.type == "http":
        client = McpHttpClient(cfg)
    elif cfg.type == "pipeline":
        client = McpPipelineClient(cfg)
    else:
        raise ValueError(f"Unsupported MCP server type {cfg.type!r} for server {cfg.name!r}")

    client._on_tools_changed = lambda _n=cfg.name: asyncio.ensure_future(
        _refresh_server_tools(_n)
    )
    await client.start()
    await client.wait_ready(timeout=30)
    return client


async def connect_to_server(name: str, cfg: McpServerConfig) -> McpClientBase:
    """Memoized connection — same name returns the same Task, only one connection created.

    Single-threaded guarantee: cache[name] = task happens BEFORE any await,
    so concurrent callers always find the cache entry and await the same Task.

    On lifecycle exit, the done callback clears the cache entry, so the next
    caller triggers a fresh connection.
    """
    if name in _connection_cache:
        return await _connection_cache[name]

    task = asyncio.create_task(_create_client(cfg))
    _connection_cache[name] = task

    try:
        client = await task

        def _on_exit(fut: asyncio.Task, _name: str = name) -> None:
            _connection_cache.pop(_name, None)

        if client._lifecycle_task is not None:
            client._lifecycle_task.add_done_callback(_on_exit)
        return client
    except Exception:
        _connection_cache.pop(name, None)
        raise


def invalidate_connection(name: str) -> None:
    """Remove a server from the connection cache, forcing reconnect on next call."""
    _connection_cache.pop(name, None)


async def _safe_close(client: McpClientBase, name: str) -> None:
    """关闭旧 client，防止 lifecycle task 变成孤儿。"""
    try:
        await client.close()
    except Exception as e:
        log.debug("[mcp] Error closing old client %s: %s", name, e)


def _invalidate_if_same(name: str, failed_client: McpClientBase) -> None:
    """只在缓存中的 client 和失败的 client 是同一个时才清除。

    避免并发 retry 时 A 清掉 B 刚建好的新连接。

    这里的 task 是 _connection_cache 中的连接建立 task（_create_client），
    不是 client._lifecycle_task。task.done() 是并发安全守卫，不是连接健康检查。

    此函数只在 call_tool 抛出 reconnectable error 时被调用，调用方已经确认
    连接不可用——不需要再检查 _lifecycle_task 是否退出。实际上 _lifecycle_task
    可能仍在运行（卡在 recv 等待），但 tool call 失败是更明显的信号，
    直接丢弃连接、异步关闭即可。

    三种情况：
    - task done + 同一个 client → pop 并异步关闭旧连接
    - task done + 不同 client → 不动（别人已经建了新连接）
    - task 还在跑 → 不动（有人正在建新连接，等它完成）
    """
    task = _connection_cache.get(name)
    if task is not None and task.done():
        try:
            cached_client = task.result()
        except Exception:
            _connection_cache.pop(name, None)
            return
        if cached_client is failed_client:
            _connection_cache.pop(name, None)
            asyncio.ensure_future(_safe_close(failed_client, name))


# ---------------------------------------------------------------------------
# Tool schema / executor / map_result factories
# ---------------------------------------------------------------------------

def _make_mcp_schema(qualified_name: str, tool: dict[str, Any]) -> dict:
    """Build a ToolDef.schema for a remote MCP tool."""
    return {
        "name": qualified_name,
        "description": f"{tool['description']}",
        "input_schema": tool["input_schema"],
    }


SAME_CONN_RETRIES = 2
RECONNECT_RETRIES = 1
BASE_RETRY_DELAY = 0.5


def _make_mcp_executor(tool_name: str, server_name: str, cfg: McpServerConfig) -> Callable:
    """Factory: returns an async executor that invokes one specific remote MCP tool.

    Each call goes through connect_to_server() which is memoize-cached — zero cost
    when connected, automatic reconnect when disconnected.

    Two-phase retry strategy:
      Phase 1 — same-connection retry with exponential backoff (0.5s, 1s, 2s…).
        Handles transient errors (network blip, brief timeout) without the cost
        of tearing down and rebuilding the MCP session.
      Phase 2 — invalidate cache, reconnect, retry once.
        If the connection is truly dead, discard it and create a fresh session.

    We only detect connection breakage at call_tool time (Python MCP SDK has no
    passive onerror/onclose like the TypeScript SDK), so this retry path is the
    sole recovery mechanism.

    Generation guard: executor captures the server's generation at creation time.
    If reload_mcp_servers increments it, stale executors abort instead of retrying
    with an outdated cfg — preventing cache pollution from old-config connections.
    """
    gen = _server_generations.get(server_name, 0)

    async def _executor(inputs: dict, ctx: Any) -> ToolResult:
        last_client: McpClientBase | None = None
        total = SAME_CONN_RETRIES + 1 + RECONNECT_RETRIES

        for attempt in range(total):
            if _server_generations.get(server_name, 0) != gen:
                raise RuntimeError(
                    f"MCP server {server_name!r} was reloaded, aborting stale retry"
                )
            try:
                last_client = await connect_to_server(server_name, cfg)
                result = await last_client.call_tool(tool_name, inputs)
                text = "\n".join(
                    c.text for c in result.content if hasattr(c, "text")
                )
                return ToolResult(
                    data={"content": text, "is_error": result.isError}
                )
            except Exception as e:
                if not _is_reconnectable_error(e) or attempt >= total - 1:
                    raise
                if attempt < SAME_CONN_RETRIES:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    log.warning(
                        "[mcp] %s.%s failed (%s), retry same connection "
                        "in %.1fs (%d/%d)",
                        server_name, tool_name, e,
                        delay, attempt + 1, SAME_CONN_RETRIES,
                    )
                    await asyncio.sleep(delay)
                elif attempt == SAME_CONN_RETRIES:
                    log.warning(
                        "[mcp] %s.%s same-connection retries exhausted, "
                        "reconnecting",
                        server_name, tool_name,
                    )
                    if last_client is not None:
                        _invalidate_if_same(server_name, last_client)

        raise RuntimeError("unreachable")
    return _executor


def _map_result(result_data: dict) -> str:
    """Map the raw result dict from MCP into a string for LLM consumption."""
    content = result_data.get("content", "")
    is_error = result_data.get("is_error", False)
    if is_error:
        return f"Error: {content}"
    return content


# ---------------------------------------------------------------------------
# Refresh tools (async) — called from tools/list_changed notification
# ---------------------------------------------------------------------------

async def _refresh_server_tools(server_name: str) -> None:
    """重新获取指定 server 的工具列表并更新注册表。"""
    srv = _servers.get(server_name)
    if srv is None or _tool_registry is None:
        return

    try:
        tool_list = await srv.client.list_tools()
    except Exception as e:
        log.error("[mcp] Failed to refresh tools for %s: %s", server_name, e)
        return

    _tool_registry.unregister_by_source(f"mcp:{server_name}")
    old_count = len(srv.tool_names)

    new_names: list[str] = []
    _server_generations.setdefault(server_name, 0)
    for sname, tools in tool_list.items():
        for tool in tools:
            qname = f"mcp__{sname}__{tool['name']}"
            td = ToolDef(
                schema=_make_mcp_schema(qname, tool),
                executor=_make_mcp_executor(tool['name'], server_name, srv.config),
                map_result=_map_result,
            )
            _tool_registry.register(td.name, td, source=f"mcp:{server_name}")
            new_names.append(qname)

    srv.tool_names = new_names
    log.info("[mcp] Refreshed %s: %d → %d tools", server_name, old_count, len(new_names))


# ---------------------------------------------------------------------------
# Connect / load / register / reload / shutdown — all async
# ---------------------------------------------------------------------------

async def _connect_servers(
    configs: list[McpServerConfig],
) -> tuple[list[tuple[McpServerConfig, McpServer, list[ToolDef]]], list[str]]:
    """Connect servers concurrently via memoize cache, then discover tools.

    All connections are started in parallel via asyncio.create_task + connect_to_server.
    """
    connected: list[tuple[McpServerConfig, McpServer, list[ToolDef]]] = []
    err_msgs: list[str] = []

    tasks: list[tuple[McpServerConfig, asyncio.Task[McpClientBase]]] = []
    for cfg in configs:
        try:
            task = asyncio.create_task(connect_to_server(cfg.name, cfg))
            tasks.append((cfg, task))
        except Exception as e:
            err_msgs.append(f"Failed to create MCP client {cfg.name!r}: {e}")

    for cfg, task in tasks:
        try:
            client = await task
            tool_list = await client.list_tools()
            tool_defs: list[ToolDef] = []
            tool_names: list[str] = []
            _server_generations.setdefault(cfg.name, 0)
            for server_name, tools in tool_list.items():
                for tool in tools:
                    qname = f"mcp__{server_name}__{tool['name']}"
                    td = ToolDef(
                        schema=_make_mcp_schema(qname, tool),
                        executor=_make_mcp_executor(tool['name'], server_name, cfg),
                        map_result=_map_result,
                    )
                    tool_defs.append(td)
                    tool_names.append(qname)
            srv = McpServer(config=cfg, client=client, tool_names=tool_names)
            connected.append((cfg, srv, tool_defs))
        except Exception as e:
            err_msgs.append(f"Failed to connect to MCP server {cfg.name!r}: {e}")
            invalidate_connection(cfg.name)
            if not task.cancelled() and task.done() and task.exception() is None:
                asyncio.ensure_future(_safe_close(task.result(), cfg.name))

    return connected, err_msgs


async def load_mcp_tools() -> tuple[list[ToolDef], list[str]]:
    """Connect all configured servers and wrap their tools as ToolDef."""
    configs = load_mcp_configs()
    connected, err_msgs = await _connect_servers(configs)

    all_tool_defs: list[ToolDef] = []
    for cfg, srv, tool_defs in connected:
        _servers[cfg.name] = srv
        all_tool_defs.extend(tool_defs)

    return all_tool_defs, err_msgs


async def register_mcp_tools(tool_registry: ToolRegistry) -> None:
    """Append MCP ToolDefs into the given ToolRegistry."""
    global _tool_registry
    _tool_registry = tool_registry

    tool_defs, _ = await load_mcp_tools()
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


async def reload_mcp_servers() -> None:
    """Diff configs by hash, disconnect stale servers, connect new ones.

    Protected by _reload_lock to prevent concurrent reloads.
    """
    if _tool_registry is None:
        log.warning("[mcp-reload] called before register_mcp_tools")
        return

    async with _get_reload_lock():
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

        for name in stale:
            _server_generations[name] = _server_generations.get(name, 0) + 1
            srv = _servers.pop(name, None)
            if srv is not None:
                try:
                    await srv.client.close()
                except Exception as e:
                    log.warning("[mcp-reload] Error closing %s: %s", name, e)
            invalidate_connection(name)
            _tool_registry.unregister_by_source(f"mcp:{name}")
            _config_hashes.pop(name, None)

        fresh_configs = [new_by_name[n] for n in fresh]
        connected, errs = await _connect_servers(fresh_configs)
        for err in errs:
            log.error("[mcp-reload] %s", err)

        for cfg, srv, tool_defs in connected:
            for td in tool_defs:
                _tool_registry.register(td.name, td, source=f"mcp:{cfg.name}")
            _servers[cfg.name] = srv
            _config_hashes[cfg.name] = new_hashes[cfg.name]
            log.info("[mcp-reload] Connected %s (%d tools)",
                     cfg.name, len(tool_defs))

        for name in (old_names & new_names) - changed:
            _config_hashes[name] = new_hashes[name]


async def shutdown_mcp() -> None:
    """Close all MCP connections — call on process exit."""
    for server in _servers.values():
        try:
            await server.client.close()
        except Exception:
            pass
    _servers.clear()
    _connection_cache.clear()
    _server_generations.clear()
