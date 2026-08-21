"""Tests for request-scoped ClickHouse client configuration overrides."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from mcp_clickhouse.mcp_server import (
    CLIENT_CONFIG_OVERRIDES_KEY,
    _get_client_config_overrides,
    create_clickhouse_client,
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
    """Set a fixed ClickHouse client override value for each tool call."""

    def __init__(self, overrides):
        self.overrides = overrides

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        ctx = get_context()
        ctx.set_state(CLIENT_CONFIG_OVERRIDES_KEY, self.overrides)
        return await call_next(context)


class QueryOverrideMiddleware(Middleware):
    """Set a distinct timeout based on each test query."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        query = context.message.arguments["query"]
        ctx = get_context()
        ctx.set_state(CLIENT_CONFIG_OVERRIDES_KEY, {"connect_timeout": int(query.split()[-1])})
        return await call_next(context)


class FakeQueryClient:
    def __init__(self, connect_timeout, barrier=None):
        self.connect_timeout = connect_timeout
        self.barrier = barrier
        self.server_settings = {}
        self.server_version = "24.10"

    def query(self, _query, settings):
        assert settings == {"readonly": "1"}
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        return SimpleNamespace(
            column_names=["connect_timeout"],
            result_rows=[(self.connect_timeout,)],
        )


class TestConfigOverrideUnit:
    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context")
    def test_overrides_merged_into_client_config(self, mock_get_context, mock_cc):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = {"connect_timeout": 99, "send_receive_timeout": 199}
        mock_get_context.return_value = mock_ctx
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client()

        call_kwargs = mock_cc.get_client.call_args.kwargs
        assert call_kwargs["connect_timeout"] == 99
        assert call_kwargs["send_receive_timeout"] == 199

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context")
    def test_empty_overrides_no_change(self, mock_get_context, mock_cc):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = {}
        mock_get_context.return_value = mock_ctx
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client()

        call_kwargs = mock_cc.get_client.call_args.kwargs
        assert "host" in call_kwargs
        assert "username" in call_kwargs

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context")
    def test_none_overrides_no_change(self, mock_get_context, mock_cc):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = None
        mock_get_context.return_value = mock_ctx
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client()

        assert "host" in mock_cc.get_client.call_args.kwargs

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_no_request_context_falls_back_to_defaults(self, mock_cc):
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client()

        assert "host" in mock_cc.get_client.call_args.kwargs

    @pytest.mark.parametrize("invalid_overrides", [[], "", 0, False])
    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context")
    def test_invalid_context_state_fails_before_client_creation(
        self, mock_get_context, mock_cc, invalid_overrides
    ):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = invalid_overrides
        mock_get_context.return_value = mock_ctx

        with pytest.raises(ToolError) as exc_info:
            create_clickhouse_client()

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

    @patch("mcp_clickhouse.mcp_server.get_context")
    def test_capture_snapshots_mappings_but_preserves_opaque_objects(self, mock_get_context):
        pool_manager = object()
        settings = {"max_threads": 2}
        state = {
            "connect_timeout": 40,
            "settings": settings,
            "pool_mgr": pool_manager,
        }
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = state
        mock_get_context.return_value = mock_ctx

        snapshot = _get_client_config_overrides()
        state["connect_timeout"] = 50
        settings["max_threads"] = 3

        assert snapshot["connect_timeout"] == 40
        assert snapshot["settings"] == {"max_threads": 2}
        assert snapshot["pool_mgr"] is pool_manager

    @patch("mcp_clickhouse.mcp_server.execute_query", return_value="result")
    @patch("mcp_clickhouse.mcp_server._get_client_config_overrides")
    def test_sync_run_query_passes_captured_overrides(self, mock_get_overrides, mock_execute):
        overrides = {"connect_timeout": 41}
        mock_get_overrides.return_value = overrides

        assert run_query("SELECT 1") == "result"

        mock_execute.assert_called_once_with("SELECT 1", overrides)

    @pytest.mark.asyncio
    @patch("mcp_clickhouse.mcp_server.execute_query", return_value="result")
    @patch("mcp_clickhouse.mcp_server._get_client_config_overrides")
    async def test_async_run_query_passes_captured_overrides(
        self, mock_get_overrides, mock_execute
    ):
        overrides = {"connect_timeout": 42}
        mock_get_overrides.return_value = overrides

        assert await run_query_async("SELECT 1") == "result"

        mock_execute.assert_called_once_with("SELECT 1", overrides)


@pytest.fixture
def mcp_server():
    return mcp


class TestConfigOverrideMcpBoundary:
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
