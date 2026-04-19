from __future__ import annotations

import asyncio
import threading
from typing import Any
import src.mcp_tool.base as base

class McpPipelineClient(base.McpClientBase):
    """MCP client for pipeline servers.

    This is a thin wrapper around McpHttpClient that translates between the
    pipeline server's expected tool schema and the MCP client's interface.
    """
    async def _async_lifecycle(self):
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        self._close_event = asyncio.Event()
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=self._cfg.command,
                        args=self._cfg.args,
                    )
                )
            )
            session: ClientSession = await stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()
            self._session = session
            if self._ready_event is not None:
                self._ready_event.set()
            await self._close_event.wait()
