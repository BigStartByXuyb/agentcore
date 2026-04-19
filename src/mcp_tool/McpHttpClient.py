from __future__ import annotations

import asyncio
import threading
from typing import Any
import src.mcp_tool.base as base

class McpHttpClient(base.McpClientBase):
    """Streamable HTTP transport for MCP servers.

    Structure mirrors StdioMcpClient (in __init__.py) — shared loop from
    outside, _async_lifecycle keeps session alive, sync wrappers forward
    calls via run_coroutine_threadsafe.
    """
    # ------ lifecycle ------
    async def _async_lifecycle(self) -> None:
        from contextlib import AsyncExitStack
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        self._close_event = asyncio.Event()

        async with AsyncExitStack() as stack:
            # streamablehttp_client 返回三元组 (read, write, get_session_id)
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(url=self._cfg.url)
            )
            session: ClientSession = await stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()
            self._session = session

            if self._ready_event is not None:
                self._ready_event.set()

            await self._close_event.wait()
        # async with 退出 → stack 自动清理

