"""Tests for request-scoped ClickHouse client configuration overrides."""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from mcp_clickhouse.mcp_server import (
    CLIENT_CONFIG_OVERRIDES_KEY,
    _active_queries,
    _active_queries_lock,
    _clear_client_cache,
    _get_client_config_overrides,
    _remove_active_query,
    create_clickhouse_client,
    list_tables_async,
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
        await ctx.set_state(
            CLIENT_CONFIG_OVERRIDES_KEY,
            self.overrides,
            serializable=False,
        )
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


class OneShotSessionOverrideMiddleware(Middleware):
    """Set a session-scoped override on the first tool call only."""

    def __init__(self):
        self.call_count = 0

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        self.call_count += 1
        if self.call_count == 1:
            ctx = get_context()
            await ctx.set_state(CLIENT_CONFIG_OVERRIDES_KEY, {"connect_timeout": 99})
        return await call_next(context)


class FakeQueryClient:
    def __init__(self, connect_timeout, barrier=None):
        self.connect_timeout = connect_timeout
        self.barrier = barrier
        self.server_settings = {}
        self.server_version = "24.10"

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

    @pytest.mark.parametrize("invalid_overrides", [[], "", 0, False])
    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_invalid_context_state_fails_before_client_creation(self, mock_cc, invalid_overrides):
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
        mock_ctx._request_state = {"request-key": state}
        mock_ctx._make_state_key.return_value = "request-key"
        mock_get_context.return_value = mock_ctx

        snapshot = _get_client_config_overrides()
        state["connect_timeout"] = 50
        settings["max_threads"] = 3

        assert snapshot["connect_timeout"] == 40
        assert snapshot["settings"] == {"max_threads": 2}
        assert snapshot["pool_mgr"] is pool_manager

    @patch("mcp_clickhouse.mcp_server.get_context")
    def test_capture_does_not_fall_back_to_session_state(self, mock_get_context):
        mock_ctx = MagicMock()
        mock_ctx._request_state = {}
        mock_ctx._make_state_key.return_value = "request-key"
        mock_ctx.get_state = AsyncMock(side_effect=AssertionError("must not read session state"))
        mock_get_context.return_value = mock_ctx

        assert _get_client_config_overrides() is None

        mock_ctx.get_state.assert_not_awaited()

    @patch("mcp_clickhouse.mcp_server.get_context", return_value=SimpleNamespace())
    def test_capture_fails_loudly_when_fastmcp_private_state_api_drifts(
        self,
        _mock_get_context,
    ):
        with pytest.raises(RuntimeError, match="request-local state API is unavailable"):
            _get_client_config_overrides()

    @patch("mcp_clickhouse.mcp_server.execute_query", return_value="result")
    @patch(
        "mcp_clickhouse.mcp_server._get_client_config_overrides",
        return_value={"connect_timeout": 41},
    )
    def test_sync_run_query_passes_resolved_config(self, _mock_get_overrides, mock_execute):
        assert run_query("SELECT 1") == "result"

        query, query_id, config = mock_execute.call_args.args
        assert query == "SELECT 1"
        assert query_id
        assert config["connect_timeout"] == 41

    @pytest.mark.asyncio
    @patch("mcp_clickhouse.mcp_server.execute_query", return_value="result")
    @patch("mcp_clickhouse.mcp_server._get_client_config_overrides_for_tool")
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
    @pytest.mark.parametrize(
        ("tool_name", "arguments", "helper_name"),
        [
            ("list_databases", {}, "_list_databases_with_config"),
            ("list_tables", {"database": "system"}, "_list_tables_with_config"),
        ],
    )
    async def test_registered_metadata_tools_receive_request_overrides(
        self,
        mcp_server,
        tool_name,
        arguments,
        helper_name,
    ):
        middleware = ConfigOverrideMiddleware({"connect_timeout": 99})
        mcp_server.add_middleware(middleware)

        def capture_config(config, *_args):
            return json.dumps({"connect_timeout": config["connect_timeout"]})

        try:
            with patch(
                f"mcp_clickhouse.mcp_server.{helper_name}",
                side_effect=capture_config,
            ) as helper:
                async with Client(mcp_server) as client:
                    result = await client.call_tool(tool_name, arguments)

            assert json.loads(result.content[0].text) == {"connect_timeout": 99}
            assert helper.call_args.args[0]["connect_timeout"] == 99
        finally:
            mcp_server.middleware.remove(middleware)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool_name", "arguments", "helper_name"),
        [
            ("list_databases", {}, "_list_databases_with_config"),
            ("list_tables", {"database": "system"}, "_list_tables_with_config"),
        ],
    )
    async def test_metadata_tools_do_not_block_event_loop(
        self,
        mcp_server,
        tool_name,
        arguments,
        helper_name,
    ):
        def slow_metadata_call(_config, *_args):
            time.sleep(0.5)
            return json.dumps({"status": "ok"})

        with patch(
            f"mcp_clickhouse.mcp_server.{helper_name}",
            side_effect=slow_metadata_call,
        ):
            async with Client(mcp_server) as client:
                slow_task = asyncio.create_task(client.call_tool(tool_name, arguments))
                await asyncio.sleep(0.05)

                start = time.perf_counter()
                tools = await client.list_tools()
                elapsed = time.perf_counter() - start

                await slow_task

        assert len(tools) >= 1
        assert elapsed < 0.3

    @pytest.mark.asyncio
    async def test_metadata_saturation_does_not_starve_query_executor(self):
        metadata_started = threading.Event()
        release_metadata = threading.Event()
        metadata_executor = ThreadPoolExecutor(max_workers=1)
        query_executor = ThreadPoolExecutor(max_workers=1)

        def blocked_metadata_call(_config, *_args):
            metadata_started.set()
            assert release_metadata.wait(timeout=2)
            return json.dumps({"status": "ok"})

        def completed_query(_query, query_id, _config):
            with _active_queries_lock:
                state = _active_queries[query_id]
            _remove_active_query(query_id, state)
            return '{"columns":["value"],"rows":[[1]]}'

        metadata_task = None
        try:
            with (
                patch("mcp_clickhouse.mcp_server.METADATA_EXECUTOR", metadata_executor),
                patch("mcp_clickhouse.mcp_server.QUERY_EXECUTOR", query_executor),
                patch(
                    "mcp_clickhouse.mcp_server._list_tables_with_config",
                    side_effect=blocked_metadata_call,
                ),
                patch(
                    "mcp_clickhouse.mcp_server.execute_query",
                    side_effect=completed_query,
                ),
            ):
                metadata_task = asyncio.create_task(list_tables_async("system"))
                assert await asyncio.to_thread(metadata_started.wait, 1)

                result = await asyncio.wait_for(run_query_async("SELECT 1"), timeout=1)

            assert json.loads(result)["rows"] == [[1]]
        finally:
            release_metadata.set()
            if metadata_task is not None:
                await asyncio.gather(metadata_task, return_exceptions=True)
            metadata_executor.shutdown(wait=True)
            query_executor.shutdown(wait=True)

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["auto", "legacy"])
    async def test_session_scoped_override_fails_with_middleware_guidance(
        self,
        mcp_server,
        mode,
    ):
        middleware = OneShotSessionOverrideMiddleware()
        mcp_server.add_middleware(middleware)
        try:
            with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client") as get_client:
                get_client.side_effect = lambda **kwargs: FakeQueryClient(
                    kwargs["connect_timeout"]
                )
                async with Client(mcp_server, mode=mode) as client:
                    with pytest.raises(ToolError) as exc_info:
                        await client.call_tool("run_query", {"query": "SELECT 1"})

            assert "serializable=False" in str(exc_info.value)
            assert CLIENT_CONFIG_OVERRIDES_KEY in str(exc_info.value)
            get_client.assert_not_called()
        finally:
            mcp_server.middleware.remove(middleware)

    @pytest.mark.asyncio
    async def test_private_request_state_drift_fails_at_mcp_boundary(self, mcp_server):
        with (
            patch(
                "mcp_clickhouse.mcp_server._request_client_config_overrides",
                side_effect=RuntimeError("FastMCP request-local state API is unavailable"),
            ),
            patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client") as get_client,
        ):
            async with Client(mcp_server) as client:
                with pytest.raises(ToolError, match="request-local state API is unavailable"):
                    await client.call_tool("run_query", {"query": "SELECT 1"})

        get_client.assert_not_called()


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
