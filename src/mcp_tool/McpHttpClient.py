from __future__ import annotations

import asyncio
import logging
from typing import Any
import src.mcp_tool.base as base

log = logging.getLogger(__name__)


class McpHttpClient(base.McpClientBase):
    """Streamable HTTP transport for MCP servers."""

    async def _async_lifecycle(self) -> None:
        from contextlib import AsyncExitStack
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        self._close_event = asyncio.Event()

        async with AsyncExitStack() as stack:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(url=self._cfg.url)
            )
            session: ClientSession = await stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()
            self._session = session

            self._register_list_changed(session)

            if self._ready_event is not None:
                self._ready_event.set()

            await self._close_event.wait()

