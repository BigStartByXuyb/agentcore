"""Tests for MCP reconnection logic (Claude Code style: memoize cache + executor retry)."""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from src.mcp_tool.base import (
    McpClientBase,
    _is_reconnectable_error,
)
from src.mcp_tool.config import McpServerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(name: str = "test-server") -> McpServerConfig:
    return McpServerConfig(
        name=name, type="http", url="http://localhost:9999",
        command="", args=[], env={}, scope="project",
    )


class _DummyClient(McpClientBase):
    """Concrete subclass for testing — _async_lifecycle is controllable."""

    def __init__(self, cfg=None):
        if cfg is None:
            cfg = _make_config()
        super().__init__(cfg)
        self.lifecycle_should_fail = False
        self.lifecycle_call_count = 0

    async def _async_lifecycle(self) -> None:
        self.lifecycle_call_count += 1
        if self.lifecycle_should_fail:
            raise ConnectionError("lifecycle failed")
        self._close_event = asyncio.Event()
        if self._ready_event is not None:
            self._ready_event.set()
        await self._close_event.wait()


# ---------------------------------------------------------------------------
# _is_reconnectable_error
# ---------------------------------------------------------------------------

class TestIsReconnectableError:
    def test_econnreset(self):
        assert _is_reconnectable_error(Exception("ECONNRESET")) is True

    def test_etimedout(self):
        assert _is_reconnectable_error(Exception("ETIMEDOUT")) is True

    def test_epipe(self):
        assert _is_reconnectable_error(Exception("EPIPE: broken pipe")) is True

    def test_econnrefused(self):
        assert _is_reconnectable_error(Exception("ECONNREFUSED")) is True

    def test_ehostunreach(self):
        assert _is_reconnectable_error(Exception("EHOSTUNREACH")) is True

    def test_connection_reset(self):
        assert _is_reconnectable_error(Exception("connection reset by peer")) is True

    def test_broken_pipe(self):
        assert _is_reconnectable_error(Exception("Broken pipe")) is True

    def test_session_not_found(self):
        assert _is_reconnectable_error(Exception("session not found")) is True

    def test_session_expired(self):
        assert _is_reconnectable_error(Exception("session expired")) is True

    def test_session_not_initialized(self):
        assert _is_reconnectable_error(RuntimeError("Session not initialized")) is True

    def test_stream_closed(self):
        assert _is_reconnectable_error(Exception("stream closed")) is True

    def test_transport_closed(self):
        assert _is_reconnectable_error(Exception("transport closed")) is True

    def test_eof(self):
        assert _is_reconnectable_error(Exception("unexpected eof")) is True

    def test_value_error_not_reconnectable(self):
        assert _is_reconnectable_error(ValueError("bad input")) is False

    def test_key_error_not_reconnectable(self):
        assert _is_reconnectable_error(KeyError("missing")) is False

    def test_generic_runtime_error_not_reconnectable(self):
        assert _is_reconnectable_error(RuntimeError("something else")) is False

    def test_permission_error_not_reconnectable(self):
        assert _is_reconnectable_error(PermissionError("access denied")) is False


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class TestCloseIdempotent:
    @pytest.mark.asyncio
    async def test_close_clears_resources(self):
        client = _DummyClient()
        await client.start()
        await client.wait_ready(timeout=5)
        assert client._lifecycle_task is not None
        assert client._close_event is not None
        await client.close()
        assert client._session is None
        assert client._lifecycle_task is None
        assert client._close_event is None

    @pytest.mark.asyncio
    async def test_double_close_safe(self):
        client = _DummyClient()
        await client.start()
        await client.wait_ready(timeout=5)
        await client.close()
        await client.close()
        assert client._session is None
        assert client._lifecycle_task is None

    @pytest.mark.asyncio
    async def test_closed_client_call_raises(self):
        client = _DummyClient()
        await client.start()
        await client.wait_ready(timeout=5)
        await client.close()
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.call_tool("test", {})


# ---------------------------------------------------------------------------
# Passive detection: _on_lifecycle_exit
# ---------------------------------------------------------------------------

class TestLifecycleExit:
    @pytest.mark.asyncio
    async def test_logs_on_unexpected_exit(self):
        """lifecycle 未经 close() 就退出 → callback 不提前返回（会记日志）。"""
        client = _DummyClient()
        await client.start()
        await client.wait_ready(timeout=5)
        assert client._close_event is not None
        assert not client._close_event.is_set()

        fut = mock.MagicMock()
        fut.result.return_value = None
        client._on_lifecycle_exit(fut)
        # 没有提前 return，fut.result() 被调用
        fut.result.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_on_expected_close(self):
        """close() 后 lifecycle 退出 → _close_event 已 set → callback 提前返回。"""
        client = _DummyClient()
        await client.start()
        await client.wait_ready(timeout=5)

        assert client._close_event is not None
        client._close_event.set()

        fut = mock.MagicMock()
        fut.result.return_value = None
        client._on_lifecycle_exit(fut)
        # 提前 return，不调 fut.result()
        fut.result.assert_not_called()

    def test_skips_when_close_event_none(self):
        """_close_event 为 None（_teardown 已完成）→ 提前返回。"""
        client = _DummyClient()
        client._close_event = None
        fut = mock.MagicMock()
        client._on_lifecycle_exit(fut)
        fut.result.assert_not_called()

    def test_handles_lifecycle_exception(self):
        """lifecycle 带异常的非预期退出也正常处理。"""
        client = _DummyClient()
        client._close_event = asyncio.Event()
        fut = mock.MagicMock()
        fut.result.side_effect = ConnectionError("crash")
        client._on_lifecycle_exit(fut)
        fut.result.assert_called_once()


# ---------------------------------------------------------------------------
# call_tool / list_tools — direct calls without reconnect
# ---------------------------------------------------------------------------

class TestCallTool:
    @pytest.mark.asyncio
    async def test_call_tool_success(self):
        client = _DummyClient()
        mock_result = mock.MagicMock()
        mock_session = mock.MagicMock()
        mock_session.call_tool = mock.AsyncMock(return_value=mock_result)
        client._session = mock_session

        result = await client.call_tool("test_tool", {"arg": "val"})
        assert result == mock_result

    @pytest.mark.asyncio
    async def test_call_tool_no_session_raises(self):
        client = _DummyClient()
        client._session = None
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.call_tool("test", {})

    @pytest.mark.asyncio
    async def test_call_tool_error_propagates(self):
        client = _DummyClient()
        client._session = mock.MagicMock()
        client._session.call_tool = mock.AsyncMock(
            side_effect=ValueError("bad input")
        )
        with pytest.raises(ValueError, match="bad input"):
            await client.call_tool("test", {})

    @pytest.mark.asyncio
    async def test_list_tools_success(self):
        client = _DummyClient()
        mock_tool = mock.MagicMock()
        mock_tool.name = "my_tool"
        mock_tool.description = "A tool"
        mock_tool.inputSchema = {"type": "object"}

        mock_result = mock.MagicMock()
        mock_result.tools = [mock_tool]

        mock_session = mock.MagicMock()
        mock_session.list_tools = mock.AsyncMock(return_value=mock_result)
        client._session = mock_session

        result = await client.list_tools()
        assert "test-server" in result
        assert len(result["test-server"]) == 1
        assert result["test-server"][0]["name"] == "my_tool"

    @pytest.mark.asyncio
    async def test_list_tools_no_session_raises(self):
        client = _DummyClient()
        client._session = None
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.list_tools()


# ---------------------------------------------------------------------------
# Memoize cache: connect_to_server
# ---------------------------------------------------------------------------

class TestConnectToServer:
    @pytest.mark.asyncio
    async def test_cache_returns_same_client(self):
        """同一 name 多次调用返回同一个 client。"""
        import src.mcp_tool as mcp_mod

        cfg = _make_config("cache-test")
        mock_client = _DummyClient(cfg)

        async def _fake_create(c):
            return mock_client

        original = mcp_mod._create_client
        mcp_mod._create_client = _fake_create
        mcp_mod._connection_cache.pop("cache-test", None)
        try:
            # Patch start/wait_ready since _DummyClient needs lifecycle
            mock_client._lifecycle_task = mock.MagicMock()
            mock_client._lifecycle_task.add_done_callback = mock.MagicMock()

            c1 = await mcp_mod.connect_to_server("cache-test", cfg)
            c2 = await mcp_mod.connect_to_server("cache-test", cfg)
            assert c1 is c2
        finally:
            mcp_mod._create_client = original
            mcp_mod._connection_cache.pop("cache-test", None)

    @pytest.mark.asyncio
    async def test_invalidate_forces_new_connection(self):
        """invalidate_connection 后下次调用创建新连接。"""
        import src.mcp_tool as mcp_mod

        cfg = _make_config("inval-test")
        call_count = 0

        async def _fake_create(c):
            nonlocal call_count
            call_count += 1
            client = _DummyClient(cfg)
            client._lifecycle_task = mock.MagicMock()
            client._lifecycle_task.add_done_callback = mock.MagicMock()
            return client

        original = mcp_mod._create_client
        mcp_mod._create_client = _fake_create
        mcp_mod._connection_cache.pop("inval-test", None)
        try:
            await mcp_mod.connect_to_server("inval-test", cfg)
            assert call_count == 1

            mcp_mod.invalidate_connection("inval-test")
            await mcp_mod.connect_to_server("inval-test", cfg)
            assert call_count == 2
        finally:
            mcp_mod._create_client = original
            mcp_mod._connection_cache.pop("inval-test", None)


# ---------------------------------------------------------------------------
# Executor retry logic
# ---------------------------------------------------------------------------

class TestExecutorRetry:
    @pytest.mark.asyncio
    async def test_same_conn_retry_succeeds(self):
        """Phase 1: transient error → retry same connection → success, no invalidation."""
        import src.mcp_tool as mcp_mod

        cfg = _make_config("retry-test")

        mock_result = mock.MagicMock()
        mock_result.content = [mock.MagicMock(text="ok")]
        mock_result.isError = False

        client = mock.MagicMock(spec=McpClientBase)
        call_count = 0

        async def _call_tool(name, args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("ECONNRESET")
            return mock_result

        client.call_tool = _call_tool

        async def _fake_connect(name, c):
            return client

        original_connect = mcp_mod.connect_to_server
        original_invalidate = mcp_mod._invalidate_if_same
        original_delay = mcp_mod.BASE_RETRY_DELAY
        invalidated_clients = []

        def _track_invalidate(name, c):
            invalidated_clients.append(c)

        mcp_mod.connect_to_server = _fake_connect
        mcp_mod._invalidate_if_same = _track_invalidate
        mcp_mod.BASE_RETRY_DELAY = 0.01
        try:
            executor = mcp_mod._make_mcp_executor("test_tool", "retry-test", cfg)
            result = await executor({}, None)
            assert result.data["content"] == "ok"
            assert call_count == 2
            assert invalidated_clients == []
        finally:
            mcp_mod.connect_to_server = original_connect
            mcp_mod._invalidate_if_same = original_invalidate
            mcp_mod.BASE_RETRY_DELAY = original_delay

    @pytest.mark.asyncio
    async def test_reconnect_after_same_conn_retries_exhausted(self):
        """Phase 1 exhausted → Phase 2: invalidate → reconnect → success."""
        import src.mcp_tool as mcp_mod

        cfg = _make_config("retry-test")

        mock_result = mock.MagicMock()
        mock_result.content = [mock.MagicMock(text="ok")]
        mock_result.isError = False

        bad_client = mock.MagicMock(spec=McpClientBase)
        bad_client.call_tool = mock.AsyncMock(side_effect=Exception("ECONNRESET"))

        good_client = mock.MagicMock(spec=McpClientBase)
        good_client.call_tool = mock.AsyncMock(return_value=mock_result)

        connect_count = 0

        async def _fake_connect(name, c):
            nonlocal connect_count
            connect_count += 1
            if connect_count <= mcp_mod.SAME_CONN_RETRIES + 1:
                return bad_client
            return good_client

        original_connect = mcp_mod.connect_to_server
        original_invalidate = mcp_mod._invalidate_if_same
        original_delay = mcp_mod.BASE_RETRY_DELAY
        invalidated_clients = []

        def _track_invalidate(name, c):
            invalidated_clients.append(c)

        mcp_mod.connect_to_server = _fake_connect
        mcp_mod._invalidate_if_same = _track_invalidate
        mcp_mod.BASE_RETRY_DELAY = 0.01
        try:
            executor = mcp_mod._make_mcp_executor("test_tool", "retry-test", cfg)
            result = await executor({}, None)
            assert result.data["content"] == "ok"
            assert invalidated_clients == [bad_client]
        finally:
            mcp_mod.connect_to_server = original_connect
            mcp_mod._invalidate_if_same = original_invalidate
            mcp_mod.BASE_RETRY_DELAY = original_delay

    @pytest.mark.asyncio
    async def test_non_reconnectable_error_no_retry(self):
        """non-reconnectable error → 直接抛，不重试。"""
        import src.mcp_tool as mcp_mod

        cfg = _make_config("no-retry-test")

        async def _fake_connect(name, c):
            client = mock.MagicMock(spec=McpClientBase)
            client.call_tool = mock.AsyncMock(
                side_effect=ValueError("bad input")
            )
            return client

        original_connect = mcp_mod.connect_to_server
        mcp_mod.connect_to_server = _fake_connect
        try:
            executor = mcp_mod._make_mcp_executor("test_tool", "no-retry-test", cfg)
            with pytest.raises(ValueError, match="bad input"):
                await executor({}, None)
        finally:
            mcp_mod.connect_to_server = original_connect

    @pytest.mark.asyncio
    async def test_max_retries_exhausted(self):
        """连续 reconnectable error 超过重试次数 → 抛出。"""
        import src.mcp_tool as mcp_mod

        cfg = _make_config("exhaust-test")

        async def _fake_connect(name, c):
            client = mock.MagicMock(spec=McpClientBase)
            client.call_tool = mock.AsyncMock(
                side_effect=Exception("ECONNRESET")
            )
            return client

        original_connect = mcp_mod.connect_to_server
        mcp_mod.connect_to_server = _fake_connect
        try:
            executor = mcp_mod._make_mcp_executor("test_tool", "exhaust-test", cfg)
            with pytest.raises(Exception, match="ECONNRESET"):
                await executor({}, None)
        finally:
            mcp_mod.connect_to_server = original_connect

    @pytest.mark.asyncio
    async def test_stale_executor_aborts_on_reload(self):
        """Generation mismatch → executor raises instead of retrying with stale cfg."""
        import src.mcp_tool as mcp_mod

        cfg = _make_config("gen-test")
        connect_calls = 0

        async def _fake_connect(name, c):
            nonlocal connect_calls
            connect_calls += 1
            client = mock.MagicMock(spec=McpClientBase)
            client.call_tool = mock.AsyncMock(
                side_effect=Exception("ECONNRESET")
            )
            return client

        original_connect = mcp_mod.connect_to_server
        original_delay = mcp_mod.BASE_RETRY_DELAY
        old_gen = mcp_mod._server_generations.get("gen-test")

        mcp_mod.connect_to_server = _fake_connect
        mcp_mod.BASE_RETRY_DELAY = 0.01
        mcp_mod._server_generations.pop("gen-test", None)
        try:
            executor = mcp_mod._make_mcp_executor("test_tool", "gen-test", cfg)

            # Simulate reload: increment generation before retry kicks in
            mcp_mod._server_generations["gen-test"] = 1

            with pytest.raises(RuntimeError, match="was reloaded"):
                await executor({}, None)

            # connect_to_server should never be called — generation check fires first
            assert connect_calls == 0
        finally:
            mcp_mod.connect_to_server = original_connect
            mcp_mod.BASE_RETRY_DELAY = original_delay
            if old_gen is not None:
                mcp_mod._server_generations["gen-test"] = old_gen
            else:
                mcp_mod._server_generations.pop("gen-test", None)

    @pytest.mark.asyncio
    async def test_normal_retry_unaffected_by_generation(self):
        """No reload → generation stays 0 → normal retry works."""
        import src.mcp_tool as mcp_mod

        cfg = _make_config("gen-normal-test")

        mock_result = mock.MagicMock()
        mock_result.content = [mock.MagicMock(text="ok")]
        mock_result.isError = False

        call_count = 0

        async def _fake_connect(name, c):
            client = mock.MagicMock(spec=McpClientBase)

            async def _call_tool(n, a):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise Exception("ECONNRESET")
                return mock_result

            client.call_tool = _call_tool
            return client

        original_connect = mcp_mod.connect_to_server
        original_delay = mcp_mod.BASE_RETRY_DELAY
        old_gen = mcp_mod._server_generations.get("gen-normal-test")

        mcp_mod.connect_to_server = _fake_connect
        mcp_mod.BASE_RETRY_DELAY = 0.01
        mcp_mod._server_generations.pop("gen-normal-test", None)
        try:
            executor = mcp_mod._make_mcp_executor("test_tool", "gen-normal-test", cfg)
            result = await executor({}, None)
            assert result.data["content"] == "ok"
            assert call_count == 2
        finally:
            mcp_mod.connect_to_server = original_connect
            mcp_mod.BASE_RETRY_DELAY = original_delay
            if old_gen is not None:
                mcp_mod._server_generations["gen-normal-test"] = old_gen
            else:
                mcp_mod._server_generations.pop("gen-normal-test", None)

    @pytest.mark.asyncio
    async def test_invalidate_if_same_skips_new_client(self):
        """_invalidate_if_same 不会清掉其他协程建的新连接。"""
        import src.mcp_tool as mcp_mod

        old_client = mock.MagicMock(spec=McpClientBase)
        new_client = mock.MagicMock(spec=McpClientBase)

        done_task = asyncio.get_event_loop().create_future()
        done_task.set_result(new_client)

        mcp_mod._connection_cache["safe-test"] = done_task
        try:
            mcp_mod._invalidate_if_same("safe-test", old_client)
            assert "safe-test" in mcp_mod._connection_cache
        finally:
            mcp_mod._connection_cache.pop("safe-test", None)


# ---------------------------------------------------------------------------
# _refresh_server_tools
# ---------------------------------------------------------------------------

class TestRefreshServerTools:
    @pytest.mark.asyncio
    async def test_refresh_updates_registry(self):
        from src.mcp_tool import _refresh_server_tools, _servers, McpServer
        from src.tools import ToolRegistry

        registry = ToolRegistry()

        import src.mcp_tool as mcp_mod
        old_tool_registry = mcp_mod._tool_registry
        mcp_mod._tool_registry = registry

        mock_client = mock.MagicMock()
        mock_client.list_tools = mock.AsyncMock(return_value={
            "test-srv": [
                {"name": "tool_a", "description": "A", "input_schema": {"type": "object"}},
                {"name": "tool_b", "description": "B", "input_schema": {"type": "object"}},
            ]
        })

        srv = McpServer(
            config=_make_config("test-srv"),
            client=mock_client,
            tool_names=["mcp__test-srv__old_tool"],
        )
        _servers["test-srv"] = srv
        registry.register(
            "mcp__test-srv__old_tool",
            mock.MagicMock(),
            source="mcp:test-srv",
        )

        try:
            await _refresh_server_tools("test-srv")
            assert "mcp__test-srv__tool_a" in registry._tools
            assert "mcp__test-srv__tool_b" in registry._tools
            assert "mcp__test-srv__old_tool" not in registry._tools
            assert srv.tool_names == ["mcp__test-srv__tool_a", "mcp__test-srv__tool_b"]
        finally:
            _servers.pop("test-srv", None)
            mcp_mod._tool_registry = old_tool_registry

    @pytest.mark.asyncio
    async def test_refresh_nonexistent_server(self):
        from src.mcp_tool import _refresh_server_tools
        await _refresh_server_tools("nonexistent")

    @pytest.mark.asyncio
    async def test_refresh_no_registry(self):
        from src.mcp_tool import _refresh_server_tools, _servers, McpServer
        import src.mcp_tool as mcp_mod

        old = mcp_mod._tool_registry
        mcp_mod._tool_registry = None
        _servers["tmp"] = McpServer(config=_make_config("tmp"), client=mock.MagicMock())
        try:
            await _refresh_server_tools("tmp")
        finally:
            _servers.pop("tmp", None)
            mcp_mod._tool_registry = old


# ---------------------------------------------------------------------------
# _register_list_changed
# ---------------------------------------------------------------------------

class TestRegisterListChanged:
    def test_registers_handler(self):
        client = _DummyClient()
        client._on_tools_changed = mock.MagicMock()

        mock_session = mock.MagicMock()
        client._register_list_changed(mock_session)

        mock_session.set_notification_handler.assert_called_once()
        call_args = mock_session.set_notification_handler.call_args
        assert call_args[0][0] == "notifications/tools/list_changed"

    def test_skips_without_callback(self):
        client = _DummyClient()
        client._on_tools_changed = None

        mock_session = mock.MagicMock()
        client._register_list_changed(mock_session)

        mock_session.set_notification_handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_calls_callback(self):
        client = _DummyClient()
        callback = mock.MagicMock()
        client._on_tools_changed = callback

        mock_session = mock.MagicMock()
        client._register_list_changed(mock_session)

        handler = mock_session.set_notification_handler.call_args[0][1]
        await handler(None)
        callback.assert_called_once()
