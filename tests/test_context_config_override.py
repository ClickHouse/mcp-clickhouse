"""Tests for context state-based ClickHouse client configuration overrides."""

import pytest
from unittest.mock import patch, MagicMock

from fastmcp import Client
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext

from mcp_clickhouse.mcp_server import (
    mcp,
    create_clickhouse_client,
    CLIENT_CONFIG_OVERRIDES_KEY,
    _client_config_overrides_var,
)


class ConfigOverrideMiddleware(Middleware):
    """Test middleware that sets ClickHouse client config overrides."""

    def __init__(self, overrides: dict):
        self.overrides = overrides

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        token = _client_config_overrides_var.set(self.overrides)
        try:
            return await call_next(context)
        finally:
            _client_config_overrides_var.reset(token)


class TestConfigOverrideUnit:
    """Unit tests for the config override merge logic in create_clickhouse_client."""

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_overrides_merged_into_client_config(self, mock_cc):
        """Verify overrides from ContextVar are merged into the client config."""
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        token = _client_config_overrides_var.set(
            {"connect_timeout": 99, "send_receive_timeout": 199}
        )
        try:
            create_clickhouse_client()
        finally:
            _client_config_overrides_var.reset(token)

        call_kwargs = mock_cc.get_client.call_args[1]
        assert call_kwargs["connect_timeout"] == 99
        assert call_kwargs["send_receive_timeout"] == 199

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_empty_overrides_no_change(self, mock_cc):
        """Empty overrides dict should not alter the base config."""
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        token = _client_config_overrides_var.set({})
        try:
            create_clickhouse_client()
        finally:
            _client_config_overrides_var.reset(token)

        call_kwargs = mock_cc.get_client.call_args[1]
        assert "host" in call_kwargs
        assert "username" in call_kwargs

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_no_overrides_in_context(self, mock_cc):
        """When ContextVar has no overrides (default None), base config is used."""
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client()

        call_kwargs = mock_cc.get_client.call_args[1]
        assert "host" in call_kwargs

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_username_override(self, mock_cc):
        """Verify username can be overridden for per-user credential passthrough."""
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        token = _client_config_overrides_var.set(
            {"username": "other_user", "password": "other_pass"}
        )
        try:
            create_clickhouse_client()
        finally:
            _client_config_overrides_var.reset(token)

        call_kwargs = mock_cc.get_client.call_args[1]
        assert call_kwargs["username"] == "other_user"
        assert call_kwargs["password"] == "other_pass"


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
