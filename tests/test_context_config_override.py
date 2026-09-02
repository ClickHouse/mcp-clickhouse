"""Tests for request-scoped ClickHouse client configuration overrides."""

import asyncio
import json
import re
import threading
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from starlette.exceptions import StarletteDeprecationWarning

with warnings.catch_warnings():
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient

from mcp_clickhouse.mcp_server import (
    CLIENT_CONFIG_OVERRIDES_KEY,
    _clear_client_cache,
    _get_client_config_overrides,
    create_clickhouse_client,
    get_config,
    mcp,
    run_query,
    run_query_async,
)


def _base_client_config(**overrides):
    config = {
        "host": "localhost",
        "port": 8123,
        "username": "default",
        "password": "secret",
        "interface": "http",
        "secure": False,
        "verify": False,
        "connect_timeout": 30,
        "send_receive_timeout": 300,
        "client_name": "mcp_clickhouse",
    }
    config.update(overrides)
    return config


def _mock_config(client_config):
    config = MagicMock()
    config.get_client_config.return_value = client_config
    return config


class ConfigOverrideMiddleware(Middleware):
    """Set a fixed ClickHouse client override value for each tool call.

    Uses the documented request-scoped pattern (serializable=False).
    """

    def __init__(self, overrides):
        self.overrides = overrides

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        ctx = get_context()
        await ctx.set_state(CLIENT_CONFIG_OVERRIDES_KEY, self.overrides, serializable=False)
        return await call_next(context)


class QueryOverrideMiddleware(Middleware):
    """Set a distinct timeout based on each test query."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        query = context.message.arguments["query"]
        ctx = get_context()
        await ctx.set_state(
            CLIENT_CONFIG_OVERRIDES_KEY,
            {"connect_timeout": int(query.split()[-1])},
            serializable=False,
        )
        return await call_next(context)


class FirstCallSessionStateMiddleware(Middleware):
    """Set overrides once with FastMCP's default session-scoped set_state.

    This is the undocumented pattern (serializable=True). The server must
    still consume the value on the first tool call rather than let it apply
    to every later call in the same MCP session.
    """

    def __init__(self, overrides):
        self.overrides = overrides
        self.calls = 0

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        self.calls += 1
        if self.calls == 1:
            await get_context().set_state(CLIENT_CONFIG_OVERRIDES_KEY, self.overrides)
        return await call_next(context)


class FakeQueryClient:
    def __init__(self, connect_timeout, barrier=None, **_kwargs):
        self.connect_timeout = connect_timeout
        self.barrier = barrier
        self.server_settings = {}
        self.server_version = "24.10"

    def close(self):
        pass

    def command(self, _query):
        return f"db_{self.connect_timeout}"

    def query(self, _query, settings):
        assert settings["readonly"] == "1"
        assert settings["query_id"]
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        return SimpleNamespace(
            column_names=["connect_timeout"],
            result_rows=[(self.connect_timeout,)],
        )


class TestConfigOverrideUnit:
    def setup_method(self):
        _clear_client_cache()

    def teardown_method(self):
        _clear_client_cache()

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_overrides_merged_into_client_config(self, mock_cc):
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client({"connect_timeout": 99, "send_receive_timeout": 199})

        call_kwargs = mock_cc.get_client.call_args.kwargs
        assert call_kwargs["connect_timeout"] == 99
        assert call_kwargs["send_receive_timeout"] == 199

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_empty_overrides_no_change(self, mock_cc):
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client({})

        call_kwargs = mock_cc.get_client.call_args.kwargs
        assert "host" in call_kwargs
        assert "username" in call_kwargs

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_none_overrides_no_change(self, mock_cc):
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client(None)

        assert "host" in mock_cc.get_client.call_args.kwargs

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_no_request_context_falls_back_to_defaults(self, mock_cc):
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client()

        assert "host" in mock_cc.get_client.call_args.kwargs

    @pytest.mark.asyncio
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    async def test_capture_returns_none_outside_request(self, _mock_get_context):
        assert await _get_client_config_overrides() is None

    @pytest.mark.asyncio
    @patch("mcp_clickhouse.mcp_server.get_context")
    async def test_capture_reads_async_context_state(self, mock_get_context):
        mock_ctx = MagicMock()
        mock_ctx.get_state = AsyncMock(return_value={"connect_timeout": 7})
        mock_ctx.delete_state = AsyncMock()
        mock_get_context.return_value = mock_ctx

        assert await _get_client_config_overrides() == {"connect_timeout": 7}
        mock_ctx.get_state.assert_awaited_once_with(CLIENT_CONFIG_OVERRIDES_KEY)
        # Consume the value so session-scoped state cannot reach a later request.
        mock_ctx.delete_state.assert_awaited_once_with(CLIENT_CONFIG_OVERRIDES_KEY)

    @pytest.mark.asyncio
    @patch("mcp_clickhouse.mcp_server.get_context")
    async def test_capture_without_state_does_not_delete(self, mock_get_context):
        mock_ctx = MagicMock()
        mock_ctx.get_state = AsyncMock(return_value=None)
        mock_ctx.delete_state = AsyncMock()
        mock_get_context.return_value = mock_ctx

        assert await _get_client_config_overrides() is None
        mock_ctx.delete_state.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("mcp_clickhouse.mcp_server.get_context")
    async def test_capture_invalid_state_fails_before_delete(self, mock_get_context):
        mock_ctx = MagicMock()
        mock_ctx.get_state = AsyncMock(return_value="do-not-expose")
        mock_ctx.delete_state = AsyncMock()
        mock_get_context.return_value = mock_ctx

        with pytest.raises(ToolError) as exc_info:
            await _get_client_config_overrides()

        assert "do-not-expose" not in str(exc_info.value)
        mock_ctx.delete_state.assert_not_awaited()

    @pytest.mark.parametrize("invalid_overrides", [[], "", 0, False])
    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_invalid_context_state_fails_before_client_creation(
        self, mock_cc, invalid_overrides
    ):
        with pytest.raises(ToolError) as exc_info:
            create_clickhouse_client(invalid_overrides)

        assert repr(invalid_overrides) not in str(exc_info.value)
        assert CLIENT_CONFIG_OVERRIDES_KEY in str(exc_info.value)
        mock_cc.get_client.assert_not_called()

    @pytest.mark.parametrize("key", ["settings", "generic_args"])
    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_invalid_nested_mapping_fails_before_client_creation(self, mock_cc, key):
        with pytest.raises(ToolError, match=rf"{key} must be a mapping"):
            create_clickhouse_client({key: "do-not-expose"})

        assert "do-not-expose" not in str(mock_cc.mock_calls)
        mock_cc.get_client.assert_not_called()

    @pytest.mark.parametrize(
        ("overrides", "rejected_key"),
        [
            ({"role": "secret-tenant-a"}, "role"),
            ({"ch_role": "secret-tenant-b"}, "ch_role"),
            ({"generic_args": {"role": "secret-tenant-c"}}, "generic_args.role"),
            ({"generic_args": {"ch_role": "secret-tenant-d"}}, "generic_args.ch_role"),
        ],
    )
    @patch("mcp_clickhouse.mcp_server.get_config")
    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_role_aliases_fail_before_config_or_client_creation(
        self, mock_cc, mock_get_config, overrides, rejected_key
    ):
        secret_value = next(
            value
            for value in (
                overrides.get("role"),
                overrides.get("ch_role"),
                overrides.get("generic_args", {}).get("role"),
                overrides.get("generic_args", {}).get("ch_role"),
            )
            if value is not None
        )

        with pytest.raises(ToolError) as exc_info:
            create_clickhouse_client(overrides)

        assert rejected_key in str(exc_info.value)
        assert secret_value not in str(exc_info.value)
        mock_get_config.assert_not_called()
        mock_cc.get_client.assert_not_called()

    @patch("mcp_clickhouse.mcp_server.get_config")
    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_nested_mappings_merge_with_base_config(self, mock_cc, mock_get_config):
        base_settings = {"role": "tenant_a", "max_block_size": 100}
        base_generic_args = {"query_limit": 10}
        mock_get_config.return_value = _mock_config(
            _base_client_config(settings=base_settings, generic_args=base_generic_args)
        )
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client(
            {
                "settings": {"max_threads": 2},
                "generic_args": {"compress": False},
            }
        )

        call_kwargs = mock_cc.get_client.call_args.kwargs
        assert call_kwargs["settings"] == {
            "role": "tenant_a",
            "max_block_size": 100,
            "max_threads": 2,
        }
        assert call_kwargs["generic_args"] == {"query_limit": 10, "compress": False}
        assert base_settings == {"role": "tenant_a", "max_block_size": 100}
        assert base_generic_args == {"query_limit": 10}

    @patch("mcp_clickhouse.mcp_server.get_config")
    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_explicit_role_override_replaces_base_role(self, mock_cc, mock_get_config):
        mock_get_config.return_value = _mock_config(
            _base_client_config(settings={"role": "tenant_a"})
        )
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client({"settings": {"role": "tenant_b"}})

        assert mock_cc.get_client.call_args.kwargs["settings"]["role"] == "tenant_b"

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_opaque_client_object_is_preserved_by_identity(self, mock_cc):
        class OpaquePoolManager:
            def __deepcopy__(self, _memo):
                raise AssertionError("must not be deep-copied")

        pool_manager = OpaquePoolManager()
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client({"pool_mgr": pool_manager})

        assert mock_cc.get_client.call_args.kwargs["pool_mgr"] is pool_manager

    @pytest.mark.asyncio
    @patch("mcp_clickhouse.mcp_server.get_context")
    async def test_capture_snapshots_mappings_but_preserves_opaque_objects(
        self, mock_get_context
    ):
        pool_manager = object()
        settings = {"max_threads": 2}
        state = {
            "connect_timeout": 40,
            "settings": settings,
            "pool_mgr": pool_manager,
        }
        mock_ctx = MagicMock()
        mock_ctx.get_state = AsyncMock(return_value=state)
        mock_ctx.delete_state = AsyncMock()
        mock_get_context.return_value = mock_ctx

        snapshot = await _get_client_config_overrides()
        state["connect_timeout"] = 50
        settings["max_threads"] = 3

        assert snapshot["connect_timeout"] == 40
        assert snapshot["settings"] == {"max_threads": 2}
        assert snapshot["pool_mgr"] is pool_manager

    @patch("mcp_clickhouse.mcp_server.execute_query", return_value="result")
    def test_sync_run_query_uses_base_config(self, mock_execute):
        """The sync helper never reads request state; it runs on the base config."""
        assert run_query("SELECT 1") == "result"

        query, query_id, config = mock_execute.call_args.args
        assert query == "SELECT 1"
        assert query_id
        assert "host" in config
        assert config.overrides_applied is False

    @pytest.mark.asyncio
    @patch("mcp_clickhouse.mcp_server.execute_query", return_value="result")
    @patch("mcp_clickhouse.mcp_server._get_client_config_overrides", new_callable=AsyncMock)
    async def test_async_run_query_passes_resolved_config(
        self, mock_get_overrides, mock_execute
    ):
        overrides = {"connect_timeout": 42}
        mock_get_overrides.return_value = overrides

        assert await run_query_async("SELECT 1") == "result"

        query, query_id, config = mock_execute.call_args.args
        assert query == "SELECT 1"
        assert query_id
        assert config["connect_timeout"] == 42


@pytest.fixture
def mcp_server():
    return mcp


class TestConfigOverrideMcpBoundary:
    def setup_method(self):
        _clear_client_cache()

    def teardown_method(self):
        _clear_client_cache()

    @pytest.mark.asyncio
    async def test_registered_run_query_receives_context_overrides(self, mcp_server):
        middleware = ConfigOverrideMiddleware({"connect_timeout": 99})
        mcp_server.add_middleware(middleware)
        try:
            with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client") as get_client:
                get_client.side_effect = lambda **kwargs: FakeQueryClient(
                    kwargs["connect_timeout"]
                )
                async with Client(mcp_server) as client:
                    result = await client.call_tool("run_query", {"query": "SELECT 1"})

            assert json.loads(result.content[0].text)["rows"] == [[99]]
            assert get_client.call_args.kwargs["connect_timeout"] == 99
        finally:
            mcp_server.middleware.remove(middleware)

    @pytest.mark.asyncio
    async def test_registered_list_databases_receives_context_overrides(self, mcp_server):
        middleware = ConfigOverrideMiddleware({"connect_timeout": 98})
        mcp_server.add_middleware(middleware)
        try:
            with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client") as get_client:
                get_client.side_effect = lambda **kwargs: FakeQueryClient(
                    kwargs["connect_timeout"]
                )
                async with Client(mcp_server) as client:
                    result = await client.call_tool("list_databases", {})

            assert json.loads(result.content[0].text) == ["db_98"]
            assert get_client.call_args.kwargs["connect_timeout"] == 98
        finally:
            mcp_server.middleware.remove(middleware)

    @pytest.mark.asyncio
    async def test_registered_list_databases_accepts_opaque_override_objects(
        self, mcp_server
    ):
        """Non-serializable overrides work through the documented request-scoped pattern."""
        pool_manager = object()
        middleware = ConfigOverrideMiddleware({"pool_mgr": pool_manager})
        mcp_server.add_middleware(middleware)
        try:
            with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client") as get_client:
                get_client.side_effect = lambda **kwargs: FakeQueryClient(**kwargs)
                async with Client(mcp_server) as client:
                    result = await client.call_tool("list_databases", {})

            assert json.loads(result.content[0].text) == ["db_30"]
            assert get_client.call_args.kwargs["pool_mgr"] is pool_manager
        finally:
            mcp_server.middleware.remove(middleware)

    @pytest.mark.asyncio
    async def test_list_tables_validation_error_names_the_tool(self, mcp_server):
        """The registered async wrapper reports the public tool name, not its own."""
        with patch("mcp_clickhouse.mcp_server._acquire_clickhouse_client") as acquire_client:
            async with Client(mcp_server) as client:
                with pytest.raises(ToolError) as exc_info:
                    await client.call_tool(
                        "list_tables", {"database": "database", "page_size": 0}
                    )

        assert "list_tables" in str(exc_info.value)
        assert "list_tables_async" not in str(exc_info.value)
        acquire_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_concurrent_run_queries_keep_overrides_isolated(self, mcp_server):
        barrier = threading.Barrier(2)
        middleware = QueryOverrideMiddleware()
        mcp_server.add_middleware(middleware)
        try:
            with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client") as get_client:
                get_client.side_effect = lambda **kwargs: FakeQueryClient(
                    kwargs["connect_timeout"], barrier
                )
                async with Client(mcp_server) as client:
                    first, second = await asyncio.gather(
                        client.call_tool("run_query", {"query": "SELECT 11"}),
                        client.call_tool("run_query", {"query": "SELECT 22"}),
                    )

            assert json.loads(first.content[0].text)["rows"] == [[11]]
            assert json.loads(second.content[0].text)["rows"] == [[22]]
        finally:
            mcp_server.middleware.remove(middleware)

    @pytest.mark.asyncio
    async def test_invalid_context_state_is_a_safe_tool_error(self, mcp_server):
        invalid_value = "do-not-expose"
        middleware = ConfigOverrideMiddleware(invalid_value)
        mcp_server.add_middleware(middleware)
        try:
            with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client") as get_client:
                async with Client(mcp_server) as client:
                    with pytest.raises(ToolError) as exc_info:
                        await client.call_tool("run_query", {"query": "SELECT 1"})

            assert CLIENT_CONFIG_OVERRIDES_KEY in str(exc_info.value)
            assert invalid_value not in str(exc_info.value)
            get_client.assert_not_called()
        finally:
            mcp_server.middleware.remove(middleware)


_MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}
_INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1"},
    },
}


def _jsonrpc_body(response):
    """Decode a JSON or single-event SSE response body from the MCP endpoint."""
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    match = re.search(r"^data: (.*)$", response.text, re.M)
    assert match is not None, response.text
    return json.loads(match.group(1))


class TestConfigOverrideHttpSession:
    """Drive real streamable HTTP with the mcp-session-id header echoed.

    fastmcp.Client does not resend the session header, so the in-memory client
    tests cannot observe FastMCP 4's session-scoped state store.
    """

    def setup_method(self):
        _clear_client_cache()

    def teardown_method(self):
        _clear_client_cache()

    def test_session_scoped_override_is_consumed_by_one_request(self, monkeypatch):
        for name in (
            "CLICKHOUSE_MCP_ALLOWED_ORIGINS",
            "CLICKHOUSE_MCP_TRUSTED_PROXIES",
            "CLICKHOUSE_MCP_AUTH_MODULE",
            "CLICKHOUSE_MCP_AUTH_TOKEN",
            "FASTMCP_SERVER_AUTH",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
        base_timeout = get_config().get_client_config()["connect_timeout"]
        assert base_timeout != 98

        middleware = FirstCallSessionStateMiddleware({"connect_timeout": 98})
        mcp.add_middleware(middleware)
        try:
            app = mcp.http_app(transport="http")
            with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client") as get_client:
                get_client.side_effect = lambda **kwargs: FakeQueryClient(**kwargs)
                with TestClient(app) as client:
                    init = client.post("/mcp", json=_INITIALIZE_REQUEST, headers=_MCP_HEADERS)
                    assert init.status_code == 200
                    session_headers = {
                        **_MCP_HEADERS,
                        "mcp-session-id": init.headers["mcp-session-id"],
                    }
                    client.post(
                        "/mcp",
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                        headers=session_headers,
                    )

                    texts = []
                    for request_id in (2, 3):
                        response = client.post(
                            "/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "method": "tools/call",
                                "params": {"name": "list_databases", "arguments": {}},
                            },
                            headers=session_headers,
                        )
                        assert response.status_code == 200
                        body = _jsonrpc_body(response)
                        assert "error" not in body, body
                        texts.append(json.loads(body["result"]["content"][0]["text"]))
                        # Force the next call to build a client from its own config.
                        _clear_client_cache()

            assert middleware.calls == 2
            assert texts[0] == ["db_98"]
            assert texts[1] == [f"db_{base_timeout}"]
        finally:
            mcp.middleware.remove(middleware)


@pytest.mark.skipif(
    not __import__("os").getenv("CLICKHOUSE_HOST"),
    reason="ClickHouse environment variables not set",
)
class TestConfigOverrideIntegration:
    @pytest.mark.asyncio
    async def test_tool_call_with_overrides(self, mcp_server):
        middleware = ConfigOverrideMiddleware({"connect_timeout": 99})
        mcp_server.add_middleware(middleware)
        try:
            async with Client(mcp_server) as client:
                result = await client.call_tool("list_databases", {})
                assert len(result.content) >= 1
        finally:
            mcp_server.middleware.remove(middleware)

    @pytest.mark.asyncio
    async def test_tool_call_without_overrides(self, mcp_server):
        async with Client(mcp_server) as client:
            result = await client.call_tool("list_databases", {})
            assert len(result.content) >= 1
