from __future__ import annotations

import asyncio
import threading
from abc import ABC, abstractmethod
from typing import Any

from src.mcp_tool.config import McpServerConfig


class McpClientBase(ABC):
    """MCP 客户端基类 — 子类只需实现 _async_lifecycle。

    共用逻辑：_connect、list_tools、call_tool、close。
    子类只负责定义 _async_lifecycle 里的连接方式（stdio/sse/http），
    在连接就绪后调 self._ready_event.set()，
    然后 await self._close_event.wait() 保持存活。
    """

    def __init__(self, cfg: McpServerConfig, loop: asyncio.AbstractEventLoop):
        self._cfg = cfg
        self._loop = loop
        self._session: Any | None = None
        self._ready_event: threading.Event | None = None
        self._close_event: asyncio.Event | None = None
        self._lifecycle_fut: Any | None = None

    # ------ 子类必须实现 ------

    @abstractmethod
    async def _async_lifecycle(self) -> None:
        pass

    # ------ 共用：连接管理（拆成 start + wait 两阶段） ------

    def start(self) -> None:
        """提交 lifecycle 到后台 loop，立即返回，不阻塞。"""
        self._ready_event = threading.Event()
        self._lifecycle_fut = asyncio.run_coroutine_threadsafe(
            self._async_lifecycle(), self._loop
        )

    def wait_ready(self, timeout: float = 30) -> None:
        """阻塞等待连接就绪。必须先调 start()。"""
        if self._ready_event is None:
            raise RuntimeError("start() not called")
        if not self._ready_event.wait(timeout=timeout):
            raise RuntimeError(
                f"MCP server {self._cfg.name!r} failed to connect within {timeout}s"
            )
        if self._lifecycle_fut.done():
            exc = self._lifecycle_fut.exception()
            if exc is not None:
                raise exc

    def close(self) -> None:
        if self._loop is not None and self._close_event is not None:
            self._loop.call_soon_threadsafe(self._close_event.set)
        if self._lifecycle_fut is not None:
            try:
                self._lifecycle_fut.result(timeout=10)
            except Exception:
                pass
        self._session = None
        self._close_event = None
        self._lifecycle_fut = None

    # ------ 共用：工具调用 ------

    def list_tools(self) -> dict[str, list[dict[str, Any]]]:
        if self._session is None:
            raise RuntimeError("Session not initialized")
        fut = asyncio.run_coroutine_threadsafe(
            self._list_tools_async(), self._loop
        )
        return fut.result(timeout=30)

    async def _list_tools_async(self) -> dict[str, list[dict[str, Any]]]:
        result = await self._session.list_tools()
        return {self._cfg.name: [
            {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in result.tools
        ]}

    def call_tool(self, name: str, arguments: dict) -> Any:
        if self._session is None:
            raise RuntimeError("Session not initialized")
        fut = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, arguments), self._loop
        )
        return fut.result(timeout=60)