"""Tests for MCP reconnection logic (Claude Code style: passive detection + lazy reconnect)."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest import mock

import pytest

from src.mcp_tool.base import (
    McpClientBase,
    _is_reconnectable_error,
    MAX_ERRORS_BEFORE_RECONNECT,
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


def _make_loop_and_thread():
    """Start an event loop on a background thread, return (loop, thread)."""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    return loop, t


def _stop_loop(loop, thread):
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


class _DummyClient(McpClientBase):
    """Concrete subclass for testing — _async_lifecycle is controllable."""

    def __init__(self, cfg=None, loop=None):
        if cfg is None:
            cfg = _make_config()
        if loop is None:
            loop = asyncio.new_event_loop()
        super().__init__(cfg, loop)
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

class TestStateMachine:
    def test_initial_state_disconnected(self):
        client = _DummyClient()
        assert client._state == "disconnected"

    def test_start_sets_disconnected(self):
        loop, t = _make_loop_and_thread()
        try:
            client = _DummyClient(loop=loop)
            client.start()
            assert client._state == "disconnected"
        finally:
            _stop_loop(loop, t)

    def test_wait_ready_sets_connected(self):
        loop, t = _make_loop_and_thread()
        try:
            client = _DummyClient(loop=loop)
            client.start()
            client.wait_ready(timeout=5)
            assert client._state == "connected"
        finally:
            client.close()
            _stop_loop(loop, t)

    def test_close_sets_closed(self):
        loop, t = _make_loop_and_thread()
        try:
            client = _DummyClient(loop=loop)
            client.start()
            client.wait_ready(timeout=5)
            client.close()
            assert client._state == "closed"
        finally:
            _stop_loop(loop, t)

    def test_closed_prevents_call(self):
        client = _DummyClient()
        client._state = "closed"
        with pytest.raises(RuntimeError, match="is closed"):
            client.call_tool("test", {})


# ---------------------------------------------------------------------------
# Passive detection: _on_lifecycle_exit
# ---------------------------------------------------------------------------

class TestLifecycleExit:
    def test_marks_disconnected_when_connected(self):
        """lifecycle 异常退出时，connected → disconnected。"""
        loop, t = _make_loop_and_thread()
        try:
            client = _DummyClient(loop=loop)
            client.start()
            client.wait_ready(timeout=5)
            assert client._state == "connected"

            # 触发 lifecycle 退出
            loop.call_soon_threadsafe(client._close_event.set)
            time.sleep(0.5)

            assert client._state == "disconnected"
        finally:
            _stop_loop(loop, t)

    def test_skips_when_already_closed(self):
        """手动 close() 后 lifecycle 退出不应改变 closed 状态。"""
        loop, t = _make_loop_and_thread()
        try:
            client = _DummyClient(loop=loop)
            client.start()
            client.wait_ready(timeout=5)
            client.close()
            assert client._state == "closed"
            # close() 已经触发了 lifecycle 退出，状态应保持 closed
            time.sleep(0.3)
            assert client._state == "closed"
        finally:
            _stop_loop(loop, t)

    def test_skips_when_disconnected(self):
        """lifecycle 退出时如果已经是 disconnected，不变。"""
        client = _DummyClient()
        client._state = "disconnected"
        fut = mock.MagicMock()
        fut.result.return_value = None
        client._on_lifecycle_exit(fut)
        assert client._state == "disconnected"

    def test_handles_lifecycle_exception(self):
        """lifecycle 带异常退出也能正常标记 disconnected。"""
        client = _DummyClient()
        client._state = "connected"
        fut = mock.MagicMock()
        fut.result.side_effect = ConnectionError("crash")
        client._on_lifecycle_exit(fut)
        assert client._state == "disconnected"


# ---------------------------------------------------------------------------
# Lazy reconnection: _ensure_connected
# ---------------------------------------------------------------------------

class TestEnsureConnected:
    def test_noop_when_connected(self):
        client = _DummyClient()
        client._state = "connected"
        client._ensure_connected()
        assert client.lifecycle_call_count == 0

    def test_raises_when_closed(self):
        client = _DummyClient()
        client._state = "closed"
        with pytest.raises(RuntimeError, match="is closed"):
            client._ensure_connected()

    def test_reconnects_when_disconnected(self):
        loop, t = _make_loop_and_thread()
        try:
            client = _DummyClient(loop=loop)
            client._state = "disconnected"
            client._ensure_connected()
            assert client._state == "connected"
            assert client.lifecycle_call_count == 1
        finally:
            client.close()
            _stop_loop(loop, t)

    def test_calls_on_tools_changed(self):
        loop, t = _make_loop_and_thread()
        try:
            client = _DummyClient(loop=loop)
            client._state = "disconnected"
            callback = mock.MagicMock()
            client._on_tools_changed = callback

            client._ensure_connected()

            assert client._state == "connected"
            callback.assert_called_once()
        finally:
            client.close()
            _stop_loop(loop, t)

    def test_resets_consecutive_errors(self):
        loop, t = _make_loop_and_thread()
        try:
            client = _DummyClient(loop=loop)
            client._state = "disconnected"
            client._consecutive_errors = 5

            client._ensure_connected()

            assert client._consecutive_errors == 0
        finally:
            client.close()
            _stop_loop(loop, t)

    def test_raises_on_timeout(self):
        loop, t = _make_loop_and_thread()
        try:
            client = _DummyClient(loop=loop)
            client._state = "disconnected"
            client.lifecycle_should_fail = True

            with pytest.raises((RuntimeError, ConnectionError)):
                client._ensure_connected()
        finally:
            _stop_loop(loop, t)


# ---------------------------------------------------------------------------
# _with_reconnect: consecutive error counting
# ---------------------------------------------------------------------------

class TestWithReconnect:
    def test_success_resets_errors(self):
        client = _DummyClient()
        client._state = "connected"
        client._consecutive_errors = 2

        result = client._with_reconnect(lambda: 42)

        assert result == 42
        assert client._consecutive_errors == 0

    def test_non_reconnectable_error_raises(self):
        client = _DummyClient()
        client._state = "connected"

        with pytest.raises(ValueError, match="bad"):
            client._with_reconnect(lambda: (_ for _ in ()).throw(ValueError("bad")))

    def test_reconnectable_error_increments_counter(self):
        client = _DummyClient()
        client._state = "connected"
        client._consecutive_errors = 0

        with pytest.raises(Exception, match="ECONNRESET"):
            client._with_reconnect(lambda: (_ for _ in ()).throw(Exception("ECONNRESET")))

        assert client._consecutive_errors == 1

    def test_threshold_triggers_reconnect_and_retry(self):
        """达到 MAX_ERRORS_BEFORE_RECONNECT 时标记 disconnected 并重试。"""
        loop, t = _make_loop_and_thread()
        try:
            client = _DummyClient(loop=loop)
            client._state = "connected"
            client._consecutive_errors = MAX_ERRORS_BEFORE_RECONNECT - 1

            call_count = 0
            def _fn():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise Exception("ECONNRESET")
                return "ok"

            result = client._with_reconnect(_fn)

            assert result == "ok"
            assert call_count == 2
            assert client._consecutive_errors == 0
            assert client._state == "connected"
        finally:
            client.close()
            _stop_loop(loop, t)

    def test_below_threshold_raises_without_reconnect(self):
        """未达到阈值时直接抛出错误，不重连。"""
        client = _DummyClient()
        client._state = "connected"
        client._consecutive_errors = 0

        with pytest.raises(Exception, match="ECONNRESET"):
            client._with_reconnect(lambda: (_ for _ in ()).throw(Exception("ECONNRESET")))

        assert client._consecutive_errors == 1
        assert client._state == "connected"
        assert client.lifecycle_call_count == 0


# ---------------------------------------------------------------------------
# Full flow: call_tool with reconnect
# ---------------------------------------------------------------------------

class TestCallToolReconnect:
    def test_call_tool_success(self):
        loop, t = _make_loop_and_thread()
        try:
            client = _DummyClient(loop=loop)
            client._state = "connected"

            mock_result = mock.MagicMock()
            mock_session = mock.MagicMock()
            mock_session.call_tool = mock.AsyncMock(return_value=mock_result)
            client._session = mock_session

            result = client.call_tool("test_tool", {"arg": "val"})
            assert result == mock_result
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=5)

    def test_non_reconnectable_error_propagates(self):
        loop, t = _make_loop_and_thread()
        try:
            client = _DummyClient(loop=loop)
            client._state = "connected"
            client._session = mock.MagicMock()
            client._session.call_tool = mock.AsyncMock(
                side_effect=ValueError("bad input")
            )

            with pytest.raises(ValueError, match="bad input"):
                client.call_tool("test", {})
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=5)


# ---------------------------------------------------------------------------
# _refresh_server_tools
# ---------------------------------------------------------------------------

class TestRefreshServerTools:
    def test_refresh_updates_registry(self):
        from src.mcp_tool import _refresh_server_tools, _servers, McpServer
        from src.tools import ToolRegistry

        registry = ToolRegistry()

        import src.mcp_tool as mcp_mod
        old_tool_registry = mcp_mod._tool_registry
        mcp_mod._tool_registry = registry

        mock_client = mock.MagicMock()
        mock_client.list_tools.return_value = {
            "test-srv": [
                {"name": "tool_a", "description": "A", "input_schema": {"type": "object"}},
                {"name": "tool_b", "description": "B", "input_schema": {"type": "object"}},
            ]
        }

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
            _refresh_server_tools("test-srv")
            assert "mcp__test-srv__tool_a" in registry._tools
            assert "mcp__test-srv__tool_b" in registry._tools
            assert "mcp__test-srv__old_tool" not in registry._tools
            assert srv.tool_names == ["mcp__test-srv__tool_a", "mcp__test-srv__tool_b"]
        finally:
            _servers.pop("test-srv", None)
            mcp_mod._tool_registry = old_tool_registry

    def test_refresh_nonexistent_server(self):
        from src.mcp_tool import _refresh_server_tools
        _refresh_server_tools("nonexistent")

    def test_refresh_no_registry(self):
        from src.mcp_tool import _refresh_server_tools, _servers, McpServer
        import src.mcp_tool as mcp_mod

        old = mcp_mod._tool_registry
        mcp_mod._tool_registry = None
        _servers["tmp"] = McpServer(config=_make_config("tmp"), client=mock.MagicMock())
        try:
            _refresh_server_tools("tmp")
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
