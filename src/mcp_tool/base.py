from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

from src.mcp_tool.config import McpServerConfig

log = logging.getLogger(__name__)

_RECONNECTABLE_KEYWORDS = [
    "econnreset", "etimedout", "epipe", "ehostunreach",
    "econnrefused", "connection reset", "broken pipe",
    "session not found", "session expired",
    "eof", "stream closed", "transport closed",
]


def _is_reconnectable_error(error: Exception) -> bool:
    msg = str(error).lower()
    if "session not initialized" in msg:
        return True
    return any(k in msg for k in _RECONNECTABLE_KEYWORDS)


class McpClientBase(ABC):
    """MCP 客户端基类 — 全 async，单事件循环，子类只需实现 _async_lifecycle。

    客户端只负责连接和调用，不做重连。重连由外层 connect_to_server()
    memoize 缓存 + executor 重试处理。
    """

    def __init__(self, cfg: McpServerConfig):
        self._cfg = cfg
        self._session: Any | None = None
        self._ready_event: asyncio.Event | None = None
        self._close_event: asyncio.Event | None = None
        self._lifecycle_task: asyncio.Task | None = None
        self._on_tools_changed: Callable[[], Awaitable[None]] | Callable[[], None] | None = None

    # ------ 子类必须实现 ------

    @abstractmethod
    async def _async_lifecycle(self) -> None:
        pass

    # ------ 连接管理 ------

    async def start(self) -> None:
        """在当前事件循环上启动 lifecycle task。

        async 但无 await：create_task 需要 running loop，
        async 确保只能在事件循环内调用。
        """
        self._ready_event = asyncio.Event()
        self._lifecycle_task = asyncio.create_task(self._async_lifecycle())
        self._lifecycle_task.add_done_callback(self._on_lifecycle_exit)

    async def wait_ready(self, timeout: float = 30) -> None:
        """等待连接就绪。必须先调 start()。"""
        if self._ready_event is None:
            raise RuntimeError("start() not called")
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"MCP server {self._cfg.name!r} failed to connect within {timeout}s"
            )
        if self._lifecycle_task is not None and self._lifecycle_task.done():
            exc = self._lifecycle_task.exception()
            if exc is not None:
                raise exc

    async def _teardown(self, timeout: float = 10) -> None:
        """关闭当前 lifecycle 并清理状态。"""
        if self._close_event is not None:
            self._close_event.set()
        if self._lifecycle_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._lifecycle_task), timeout=timeout
                )
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        self._session = None
        self._close_event = None
        self._lifecycle_task = None

    async def close(self) -> None:
        await self._teardown()

    # ------ 被动检测：lifecycle 退出回调 ------

    def _on_lifecycle_exit(self, fut: asyncio.Task) -> None:
        """lifecycle task 退出时被调用（sync done callback）。

        lifecycle 退出时清 memoize 缓存。

        判断预期退出：_close_event 为 None（_teardown 已完成）或已 set
        （close() 被调用，_teardown 正在等 lifecycle 退出）。
        非预期退出：_close_event 存在但未 set — lifecycle 自行崩溃。
        """
        if self._close_event is None or self._close_event.is_set():
            return
        try:
            fut.result()
        except Exception as e:
            log.warning("[mcp] %s lifecycle exited with error: %s", self._cfg.name, e)
        log.info("[mcp] %s disconnected (lifecycle exited unexpectedly)", self._cfg.name)

    # ------ 通知注册 ------

    def _register_list_changed(self, session: Any) -> None:
        """注册 tools/list_changed 通知 handler。"""
        if self._on_tools_changed is None:
            return
        callback = self._on_tools_changed
        server_name = self._cfg.name

        async def _handle_tools_changed(params: Any) -> None:
            log.info("[mcp] tools/list_changed from %s", server_name)
            try:
                result = callback()
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    await result
            except Exception as e:
                log.warning("[mcp] tools_changed callback failed: %s", e)

        try:
            session.set_notification_handler(
                "notifications/tools/list_changed",
                _handle_tools_changed,
            )
        except Exception as e:
            log.debug("[mcp] Could not register list_changed handler for %s: %s",
                      server_name, e)

    # ------ 工具调用（纯调用，不做重连） ------

    async def list_tools(self) -> dict[str, list[dict[str, Any]]]:
        if self._session is None:
            raise RuntimeError("Session not initialized")
        result = await self._session.list_tools()
        return {self._cfg.name: [
            {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in result.tools
        ]}

    async def call_tool(self, name: str, arguments: dict) -> Any:
        if self._session is None:
            raise RuntimeError("Session not initialized")
        return await self._session.call_tool(name, arguments)
