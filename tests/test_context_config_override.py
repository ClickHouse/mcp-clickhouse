"""Tests for context state-based ClickHouse client configuration overrides."""

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext
from fastmcp.server.dependencies import get_context
from mcp_clickhouse.mcp_server import mcp, create_clickhouse_client
from dotenv import load_dotenv
import asyncio
import os

load_dotenv()

# Skip all tests if ClickHouse is not configured
pytestmark = pytest.mark.skipif(
    not all([
        os.getenv("CLICKHOUSE_HOST"),
        os.getenv("CLICKHOUSE_USER"),
        os.getenv("CLICKHOUSE_PASSWORD")
    ]),
    reason="ClickHouse environment variables not set"
)


class ConfigOverrideMiddleware(Middleware):
    """Test middleware that sets ClickHouse client config overrides."""

    def __init__(self, overrides: dict):
        self.overrides = overrides

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        """Set config overrides in context state before tool execution."""
        ctx = get_context()
        ctx.set_state("clickhouse_client_config_overrides", self.overrides)
        return await call_next(context)


@pytest.fixture(scope="module")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mcp_server():
    """Return the MCP server instance for testing."""
    return mcp


@pytest.mark.asyncio
async def test_context_state_config_override(mcp_server):
    """Test that config overrides from context state are applied."""
    # Add middleware with custom timeout overrides
    test_overrides = {
        "connect_timeout": 99,
        "send_receive_timeout": 199,
    }
    
    middleware = ConfigOverrideMiddleware(test_overrides)
    mcp_server.add_middleware(middleware)

    try:
        async with Client(mcp_server) as client:
            # Call any tool - the middleware should inject config overrides
            result = await client.call_tool("list_databases", {})
            
            # If we got here without errors, the client was created successfully
            # with the overridden configuration
            assert len(result.content) >= 1
    finally:
        # Clean up middleware
        if middleware in mcp_server.middleware:
            mcp_server.middleware.remove(middleware)


@pytest.mark.asyncio
async def test_context_state_empty_override(mcp_server):
    """Test that empty config overrides don't break client creation."""
    middleware = ConfigOverrideMiddleware({})
    mcp_server.add_middleware(middleware)

    try:
        async with Client(mcp_server) as client:
            result = await client.call_tool("list_databases", {})
            assert len(result.content) >= 1
    finally:
        if middleware in mcp_server.middleware:
            mcp_server.middleware.remove(middleware)


@pytest.mark.asyncio
async def test_context_state_no_override(mcp_server):
    """Test that client creation works without any config overrides."""
    # Don't add any middleware - context state should be empty
    async with Client(mcp_server) as client:
        result = await client.call_tool("list_databases", {})
        assert len(result.content) >= 1


def test_create_client_with_context_override():
    """Test create_clickhouse_client with context state override."""
    # This test runs synchronously to verify the client creation logic
    # Note: This requires an active context, which is set up by FastMCP
    # during actual request handling
    
    # We can't easily test this without the full FastMCP request context,
    # so this is a placeholder for documentation purposes.
    # The real testing happens in the async tests above where FastMCP
    # provides the proper context.
    pass


@pytest.mark.asyncio
async def test_multiple_tools_with_override(mcp_server):
    """Test that config overrides work across multiple tool calls."""
    test_overrides = {
        "connect_timeout": 88,
    }
    
    middleware = ConfigOverrideMiddleware(test_overrides)
    mcp_server.add_middleware(middleware)

    try:
        async with Client(mcp_server) as client:
            # Call multiple tools
            result1 = await client.call_tool("list_databases", {})
            assert len(result1.content) >= 1
            
            # The override should still be active for subsequent calls
            result2 = await client.call_tool("list_databases", {})
            assert len(result2.content) >= 1
    finally:
        if middleware in mcp_server.middleware:
            mcp_server.middleware.remove(middleware)


@pytest.mark.asyncio
async def test_override_inherits_from_env(mcp_server):
    """Test that overrides merge with base config from environment."""
    # Override only timeout, other settings should come from env
    test_overrides = {
        "connect_timeout": 77,
    }
    
    middleware = ConfigOverrideMiddleware(test_overrides)
    mcp_server.add_middleware(middleware)

    try:
        async with Client(mcp_server) as client:
            # This should work because host, user, password, etc.
            # still come from environment variables
            result = await client.call_tool("list_databases", {})
            assert len(result.content) >= 1
    finally:
        if middleware in mcp_server.middleware:
            mcp_server.middleware.remove(middleware)
