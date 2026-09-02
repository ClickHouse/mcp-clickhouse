"""Tests for request-scoped ClickHouse client configuration overrides."""

import asyncio
import json
import threading
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.error_handling import RetryMiddleware
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
from tests.helpers import (
    INITIALIZE_REQUEST,
    MCP_HEADERS,
    clear_http_env,
    fake_clickhouse_client,
    jsonrpc_body,
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


def _mock_context_with_state(state):
    """A FastMCP Context stand-in whose async state methods record their calls."""
    mock_ctx = MagicMock()
    mock_ctx.get_state = AsyncMock(return_value=state)
    mock_ctx.delete_state = AsyncMock()
    mock_ctx.set_state = AsyncMock()
    return mock_ctx


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
    def __init__(self, connect_timeout, barrier=None, fail_first_command=False, **_kwargs):
        self.connect_timeout = connect_timeout
        self.barrier = barrier
        self.fail_first_command = fail_first_command
        self.server_settings = {}
        self.server_version = "24.10"

    def close(self):
        pass

    def command(self, _query):
        if self.fail_first_command:
            self.fail_first_command = False
            # Not connection-like, so the helper's own retry does not swallow it.
            raise RuntimeError("transient failure")
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
    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_overrides_merged_into_client_config(self, mock_cc):
        mock_cc.get_client.return_value = fake_clickhouse_client("24.1")

        create_clickhouse_client({"connect_timeout": 99, "send_receive_timeout": 199})

        call_kwargs = mock_cc.get_client.call_args.kwargs
        assert call_kwargs["connect_timeout"] == 99
        assert call_kwargs["send_receive_timeout"] == 199

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_empty_overrides_no_change(self, mock_cc):
        mock_cc.get_client.return_value = fake_clickhouse_client("24.1")

        create_clickhouse_client({})

        call_kwargs = mock_cc.get_client.call_args.kwargs
        assert "host" in call_kwargs
        assert "username" in call_kwargs

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_none_overrides_no_change(self, mock_cc):
        mock_cc.get_client.return_value = fake_clickhouse_client("24.1")

        create_clickhouse_client(None)

        assert "host" in mock_cc.get_client.call_args.kwargs

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_no_request_context_falls_back_to_defaults(self, mock_cc):
        mock_cc.get_client.return_value = fake_clickhouse_client("24.1")

        create_clickhouse_client()

        assert "host" in mock_cc.get_client.call_args.kwargs

    @pytest.mark.asyncio
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    async def test_capture_returns_none_outside_request(self, _mock_get_context):
        assert await _get_client_config_overrides() is None

    @pytest.mark.asyncio
    @patch("mcp_clickhouse.mcp_server.get_context")
    async def test_capture_reads_async_context_state(self, mock_get_context):
        mock_ctx = _mock_context_with_state({"connect_timeout": 7})
        mock_get_context.return_value = mock_ctx

        assert await _get_client_config_overrides() == {"connect_timeout": 7}
        mock_ctx.get_state.assert_awaited_once_with(CLIENT_CONFIG_OVERRIDES_KEY)
        # Consume any session-scoped copy, then keep a request-scoped copy so a
        # retry inside the same request sees the same overrides.
        assert mock_ctx.mock_calls == [
            call.get_state(CLIENT_CONFIG_OVERRIDES_KEY),
            call.delete_state(CLIENT_CONFIG_OVERRIDES_KEY),
            call.set_state(
                CLIENT_CONFIG_OVERRIDES_KEY, {"connect_timeout": 7}, serializable=False
            ),
        ]

    @pytest.mark.asyncio
    @patch("mcp_clickhouse.mcp_server.get_context")
    async def test_capture_without_state_does_not_write(self, mock_get_context):
        mock_ctx = _mock_context_with_state(None)
        mock_get_context.return_value = mock_ctx

        assert await _get_client_config_overrides() is None
        mock_ctx.delete_state.assert_not_awaited()
        mock_ctx.set_state.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("mcp_clickhouse.mcp_server.get_context")
    async def test_capture_invalid_state_is_consumed_then_fails(self, mock_get_context):
        """A bad session-scoped value fails this request but cannot poison the session."""
        mock_ctx = _mock_context_with_state("do-not-expose")
        mock_get_context.return_value = mock_ctx

        with pytest.raises(ToolError) as exc_info:
            await _get_client_config_overrides()

        assert "do-not-expose" not in str(exc_info.value)
        mock_ctx.delete_state.assert_awaited_once_with(CLIENT_CONFIG_OVERRIDES_KEY)
        mock_ctx.set_state.assert_awaited_once_with(
            CLIENT_CONFIG_OVERRIDES_KEY, "do-not-expose", serializable=False
        )

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
        mock_cc.get_client.return_value = fake_clickhouse_client("24.1")

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
        mock_cc.get_client.return_value = fake_clickhouse_client("24.1")

        create_clickhouse_client({"settings": {"role": "tenant_b"}})

        assert mock_cc.get_client.call_args.kwargs["settings"]["role"] == "tenant_b"

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_opaque_client_object_is_preserved_by_identity(self, mock_cc):
        class OpaquePoolManager:
            def __deepcopy__(self, _memo):
                raise AssertionError("must not be deep-copied")

        pool_manager = OpaquePoolManager()
        mock_cc.get_client.return_value = fake_clickhouse_client("24.1")

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
        mock_get_context.return_value = _mock_context_with_state(state)

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

    @pytest.mark.asyncio
    async def test_retried_tool_call_keeps_request_scoped_overrides(self, mcp_server):
        """A retry inside one MCP request must see the overrides again.

        Reading the overrides consumes any session-scoped copy; the request-scoped
        copy must survive so FastMCP's RetryMiddleware does not silently rerun the
        tool against the base configuration.
        """
        seen_timeouts = []
        override_middleware = ConfigOverrideMiddleware({"connect_timeout": 88})
        retry_middleware = RetryMiddleware(
            max_retries=1, base_delay=0, retry_exceptions=(RuntimeError,)
        )
        mcp_server.add_middleware(override_middleware)
        mcp_server.add_middleware(retry_middleware)

        def fake_get_client(**kwargs):
            fake = FakeQueryClient(kwargs["connect_timeout"], fail_first_command=True)
            original_command = fake.command

            def recording_command(query):
                seen_timeouts.append(fake.connect_timeout)
                return original_command(query)

            fake.command = recording_command
            return fake

        try:
            with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client") as get_client:
                get_client.side_effect = fake_get_client
                async with Client(mcp_server) as client:
                    result = await client.call_tool("list_databases", {})

            assert json.loads(result.content[0].text) == ["db_88"]
            assert seen_timeouts == [88, 88]
            for call_args in get_client.call_args_list:
                assert call_args.kwargs["connect_timeout"] == 88
        finally:
            mcp_server.middleware.remove(retry_middleware)
            mcp_server.middleware.remove(override_middleware)


class TestConfigOverrideHttpSession:
    """Drive real streamable HTTP with the mcp-session-id header echoed.

    fastmcp.Client does not resend the session header, so the in-memory client
    tests cannot observe FastMCP 4's session-scoped state store.
    """

    def _run_session_calls(self, monkeypatch, first_call_state, call_count):
        """Initialize one streamable HTTP session and call list_databases repeatedly.

        Returns the decoded JSON-RPC result objects in call order and the base
        connect_timeout the tool falls back to without overrides.
        """
        clear_http_env(monkeypatch)
        monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
        base_timeout = get_config().get_client_config()["connect_timeout"]
        assert base_timeout != 98

        middleware = FirstCallSessionStateMiddleware(first_call_state)
        mcp.add_middleware(middleware)
        results = []
        try:
            app = mcp.http_app(transport="http")
            with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client") as get_client:
                get_client.side_effect = lambda **kwargs: FakeQueryClient(**kwargs)
                with TestClient(app) as client:
                    init = client.post("/mcp", json=INITIALIZE_REQUEST, headers=MCP_HEADERS)
                    assert init.status_code == 200
                    session_headers = {
                        **MCP_HEADERS,
                        "mcp-session-id": init.headers["mcp-session-id"],
                    }
                    client.post(
                        "/mcp",
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                        headers=session_headers,
                    )

                    for request_id in range(2, 2 + call_count):
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
                        body = jsonrpc_body(response)
                        assert "error" not in body, body
                        results.append(body["result"])
                        # Force the next call to build a client from its own config.
                        _clear_client_cache()

            assert middleware.calls == call_count
        finally:
            mcp.middleware.remove(middleware)
        return results, base_timeout

    @staticmethod
    def _databases(result):
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])

    def test_session_scoped_override_is_consumed_by_one_request(self, monkeypatch):
        results, base_timeout = self._run_session_calls(
            monkeypatch, {"connect_timeout": 98}, call_count=2
        )

        assert self._databases(results[0]) == ["db_98"]
        assert self._databases(results[1]) == [f"db_{base_timeout}"]

    def test_invalid_session_scoped_override_does_not_poison_the_session(self, monkeypatch):
        """A bad value fails the call that finds it, then later calls use the base config."""
        results, base_timeout = self._run_session_calls(monkeypatch, "not-a-dict", call_count=3)

        assert results[0].get("isError") is True, results[0]
        error_text = results[0]["content"][0]["text"]
        assert CLIENT_CONFIG_OVERRIDES_KEY in error_text
        assert "not-a-dict" not in error_text
        assert self._databases(results[1]) == [f"db_{base_timeout}"]
        assert self._databases(results[2]) == [f"db_{base_timeout}"]


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
