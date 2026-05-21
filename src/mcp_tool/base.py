from __future__ import annotations

import asyncio
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable

from src.mcp_tool.config import McpServerConfig

log = logging.getLogger(__name__)

MAX_ERRORS_BEFORE_RECONNECT = 3
RECONNECT_TIMEOUT_S = 30

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
    """MCP 客户端基类 — 子类只需实现 _async_lifecycle。

    重连策略采用 Claude Code 的模式：
      1. 被动检测：lifecycle 异常退出时通过回调标记 disconnected
      2. 懒重连：下次调用时 _ensure_connected() 发现断开才重建连接
      3. 累计错误：工具调用连续 N 次终端错误后标记 disconnected
    """

    def __init__(self, cfg: McpServerConfig, loop: asyncio.AbstractEventLoop):
        self._cfg = cfg
        self._loop = loop
        self._session: Any | None = None
        self._ready_event: threading.Event | None = None
        self._close_event: asyncio.Event | None = None
        self._lifecycle_fut: Any | None = None
        self._state: str = "disconnected"
        self._consecutive_errors: int = 0
        self._on_tools_changed: Callable | None = None

    # ------ 子类必须实现 ------

    @abstractmethod
    async def _async_lifecycle(self) -> None:
        pass

    # ------ 连接管理 ------

    def start(self) -> None:
        """提交 lifecycle 到后台 loop，立即返回，不阻塞。"""
        self._ready_event = threading.Event()
        self._lifecycle_fut = asyncio.run_coroutine_threadsafe(
            self._async_lifecycle(), self._loop
        )
        self._lifecycle_fut.add_done_callback(self._on_lifecycle_exit)
        self._state = "disconnected"

    def wait_ready(self, timeout: float = 30) -> None:
        """阻塞等待连接就绪。必须先调 start()。"""
        if self._ready_event is None:
            raise RuntimeError("start() not called")
        if not self._ready_event.wait(timeout=timeout):
            raise RuntimeError(
                f"MCP server {self._cfg.name!r} failed to connect within {timeout}s"
            )
        if self._lifecycle_fut is not None and self._lifecycle_fut.done():
            exc = self._lifecycle_fut.exception()
            if exc is not None:
                raise exc
        self._state = "connected"
        self._consecutive_errors = 0

    def _teardown(self, timeout: float = 10) -> None:
        """关闭当前 lifecycle 并清理状态，供 close() 和 _ensure_connected() 复用。"""
        if self._loop is not None and self._close_event is not None:
            self._loop.call_soon_threadsafe(self._close_event.set)
        if self._lifecycle_fut is not None:
            try:
                self._lifecycle_fut.result(timeout=timeout)
            except Exception:
                pass
        self._session = None
        self._close_event = None
        self._lifecycle_fut = None

    def close(self) -> None:
        self._state = "closed"
        self._teardown()

    # ------ 被动检测：lifecycle 退出回调 ------

    def _on_lifecycle_exit(self, fut: Any) -> None:
        """lifecycle 协程退出时被调用。如果仍是 connected 状态，标记为 disconnected。

        对应 Claude Code 的 client.onclose → 清 memoize 缓存。
        """
        if self._state != "connected":
            return
        try:
            fut.result()
        except Exception as e:
            log.warning("[mcp] %s lifecycle exited with error: %s", self._cfg.name, e)
        self._state = "disconnected"
        log.info("[mcp] %s marked disconnected (lifecycle exited)", self._cfg.name)

    # ------ 懒重连 ------

    def _ensure_connected(self) -> None:
        """如果未连接，执行单次重连。对应 Claude Code 的 ensureConnectedClient()。"""
        if self._state == "connected":
            return
        if self._state == "closed":
            raise RuntimeError(f"MCP server {self._cfg.name!r} is closed")

        log.info("[mcp] Reconnecting %s ...", self._cfg.name)
        self._teardown(timeout=5)
        self.start()
        self.wait_ready(timeout=RECONNECT_TIMEOUT_S)
        log.info("[mcp] Reconnected %s successfully", self._cfg.name)

        if self._on_tools_changed:
            try:
                self._on_tools_changed()
            except Exception as e:
                log.warning("[mcp] on_tools_changed callback failed: %s", e)

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
                callback()
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

    # ------ 工具调用 ------

    def list_tools(self) -> dict[str, list[dict[str, Any]]]:
        return self._with_reconnect(self._list_tools_impl)

    def _list_tools_impl(self) -> dict[str, list[dict[str, Any]]]:
        if self._session is None:
            raise RuntimeError("Session not initialized")
        fut = asyncio.run_coroutine_threadsafe(
            self._list_tools_async(), self._loop
        )
        return fut.result(timeout=30)

    async def _list_tools_async(self) -> dict[str, list[dict[str, Any]]]:
        if self._session is None:
            raise RuntimeError("Session not initialized")
        result = await self._session.list_tools()
        return {self._cfg.name: [
            {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in result.tools
        ]}

    def call_tool(self, name: str, arguments: dict) -> Any:
        return self._with_reconnect(lambda: self._call_tool_impl(name, arguments))

    def _call_tool_impl(self, name: str, arguments: dict) -> Any:
        if self._session is None:
            raise RuntimeError("Session not initialized")
        fut = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, arguments), self._loop
        )
        return fut.result(timeout=60)

    # ------ 重连包装器 ------

    def _with_reconnect(self, fn: Callable[[], Any]) -> Any:
        """ensure_connected → fn()。连续终端错误达到阈值后标记断开并重试。

        对应 Claude Code 的 onerror 累计检测 + onclose 清缓存 + ensureConnectedClient 懒重连。
        """
        self._ensure_connected()
        try:
            result = fn()
            self._consecutive_errors = 0
            return result
        except Exception as e:
            if not _is_reconnectable_error(e):
                raise

            self._consecutive_errors += 1
            log.warning(
                "[mcp] Terminal error %d/%d on %s: %s",
                self._consecutive_errors, MAX_ERRORS_BEFORE_RECONNECT,
                self._cfg.name, e,
            )

            if self._consecutive_errors >= MAX_ERRORS_BEFORE_RECONNECT:
                self._state = "disconnected"
                self._consecutive_errors = 0
                self._ensure_connected()
                return fn()
            raise
