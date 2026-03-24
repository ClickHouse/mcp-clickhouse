"""Tests for context state-based ClickHouse client configuration overrides."""

import pytest
from unittest.mock import patch, MagicMock

from fastmcp import Client
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext
from fastmcp.server.dependencies import get_context

from mcp_clickhouse.mcp_server import (
    mcp,
    create_clickhouse_client,
    CLIENT_CONFIG_OVERRIDES_KEY,
)


class ConfigOverrideMiddleware(Middleware):
    """Test middleware that sets ClickHouse client config overrides."""

    def __init__(self, overrides: dict):
        self.overrides = overrides

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        ctx = get_context()
        ctx.set_state(CLIENT_CONFIG_OVERRIDES_KEY, self.overrides)
        return await call_next(context)


class TestConfigOverrideUnit:
    """Unit tests for the config override merge logic in create_clickhouse_client."""

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context")
    def test_overrides_merged_into_client_config(self, mock_get_context, mock_cc):
        """Verify overrides from context state are merged into the client config."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = {"connect_timeout": 99, "send_receive_timeout": 199}
        mock_get_context.return_value = mock_ctx
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client()

        call_kwargs = mock_cc.get_client.call_args[1]
        assert call_kwargs["connect_timeout"] == 99
        assert call_kwargs["send_receive_timeout"] == 199

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context")
    def test_empty_overrides_no_change(self, mock_get_context, mock_cc):
        """Empty overrides dict should not alter the base config."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = {}
        mock_get_context.return_value = mock_ctx
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client()

        call_kwargs = mock_cc.get_client.call_args[1]
        # Base config values from env should pass through unchanged
        assert "host" in call_kwargs
        assert "username" in call_kwargs

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context")
    def test_no_overrides_in_context(self, mock_get_context, mock_cc):
        """When context state has no overrides, base config is used as-is."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = None
        mock_get_context.return_value = mock_ctx
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client()

        call_kwargs = mock_cc.get_client.call_args[1]
        assert "host" in call_kwargs

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_no_request_context_falls_back_to_defaults(self, mock_cc):
        """Outside a request context (RuntimeError), base config is used."""
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        # get_context is NOT mocked, so it will raise RuntimeError
        # since there's no active FastMCP request context
        create_clickhouse_client()

        call_kwargs = mock_cc.get_client.call_args[1]
        assert "host" in call_kwargs


@pytest.fixture
def mcp_server():
    """Return the MCP server instance for testing."""
    return mcp


@pytest.mark.skipif(
    not __import__("os").getenv("CLICKHOUSE_HOST"),
    reason="ClickHouse environment variables not set",
)
class TestConfigOverrideIntegration:
    """Integration tests that verify overrides work end-to-end with a real ClickHouse."""

    @pytest.mark.asyncio
    async def test_tool_call_with_overrides(self, mcp_server):
        """Config overrides from middleware are applied during tool execution."""
        middleware = ConfigOverrideMiddleware({"connect_timeout": 99})
        mcp_server.add_middleware(middleware)
        try:
            async with Client(mcp_server) as client:
                result = await client.call_tool("list_databases", {})
                assert len(result.content) >= 1
        finally:
            if middleware in mcp_server.middleware:
                mcp_server.middleware.remove(middleware)

    @pytest.mark.asyncio
    async def test_tool_call_without_overrides(self, mcp_server):
        """Client creation works normally without any override middleware."""
        async with Client(mcp_server) as client:
            result = await client.call_tool("list_databases", {})
            assert len(result.content) >= 1
