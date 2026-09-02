"""Tests for the example_middleware.py module documented in the README.

These tests exercise example_middleware.py through a fresh fastmcp.FastMCP
instance and never touch the module singleton mcp_clickhouse.mcp_server.mcp.
"""

import logging
import os
import sys
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.context import Context
from fastmcp.server.dependencies import get_context

import mcp_clickhouse.mcp_middleware_hook
from mcp_clickhouse.mcp_server import CLIENT_CONFIG_OVERRIDES_KEY

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OVERRIDES = {"connect_timeout": 60, "send_receive_timeout": 120}


@pytest.fixture
def example_middleware_module(monkeypatch: pytest.MonkeyPatch):
    """Make the repo-root example_middleware.py importable, then clean up.

    A pre-existing cached example_middleware module is removed through
    monkeypatch so it is put back on teardown; the module imported during
    the test is popped directly, because monkeypatch.delitem on a key the
    test added would be undone and would leak the module into later tests.
    """
    monkeypatch.delitem(sys.modules, "example_middleware", raising=False)
    monkeypatch.syspath_prepend(REPO_ROOT)
    yield
    sys.modules.pop("example_middleware", None)


class TestExampleMiddlewareLoading:
    def test_setup_middleware_registers_all_four_in_order(
        self, monkeypatch: pytest.MonkeyPatch, example_middleware_module
    ):
        monkeypatch.setenv("MCP_MIDDLEWARE_MODULE", "example_middleware")
        server = FastMCP("t")
        initial_count = len(server.middleware)

        mcp_clickhouse.mcp_middleware_hook.setup_middleware(server)

        import example_middleware as em

        registered = server.middleware[initial_count:]
        assert [type(m) for m in registered] == [
            em.LoggingMiddleware,
            em.ToolCallLoggingMiddleware,
            em.TimingMiddleware,
            em.ClientConfigOverrideMiddleware,
        ]


class TestClientConfigOverrideMiddleware:
    @pytest.mark.asyncio
    async def test_override_visible_to_tool_call(
        self, monkeypatch: pytest.MonkeyPatch, example_middleware_module
    ):
        import example_middleware as em

        server = FastMCP("t")
        server.add_middleware(em.ClientConfigOverrideMiddleware())

        @server.tool
        async def read_overrides():
            ctx = get_context()
            return await ctx.get_state(CLIENT_CONFIG_OVERRIDES_KEY)

        async with Client(server) as client:
            result = await client.call_tool("read_overrides", {})

        assert result.data == OVERRIDES

    @pytest.mark.asyncio
    async def test_override_is_set_with_serializable_false(
        self, monkeypatch: pytest.MonkeyPatch, example_middleware_module
    ):
        """serializable=False is mandatory; spy on Context.set_state to prove it.

        FastMCP 4's default set_state is session-scoped (24 hour TTL keyed by
        mcp-session-id), so a value set without serializable=False would leak
        into every later tool call in the same HTTP session.
        """
        import example_middleware as em

        server = FastMCP("t")
        server.add_middleware(em.ClientConfigOverrideMiddleware())

        @server.tool
        async def noop_tool():
            return "ok"

        with patch.object(
            Context, "set_state", autospec=True, side_effect=Context.set_state
        ) as spy:
            async with Client(server) as client:
                await client.call_tool("noop_tool", {})

        matching_calls = [
            c for c in spy.call_args_list if c.args[1] == CLIENT_CONFIG_OVERRIDES_KEY
        ]
        assert len(matching_calls) == 1
        assert matching_calls[0].args[2] == OVERRIDES
        assert matching_calls[0].kwargs == {"serializable": False}


class TestExampleMiddlewareDoesNotBreakToolCalls:
    @pytest.mark.asyncio
    async def test_all_four_middleware_allow_a_tool_call_through(
        self, monkeypatch: pytest.MonkeyPatch, example_middleware_module, caplog
    ):
        import example_middleware as em

        server = FastMCP("t")
        server.add_middleware(em.LoggingMiddleware())
        server.add_middleware(em.ToolCallLoggingMiddleware())
        server.add_middleware(em.TimingMiddleware())
        server.add_middleware(em.ClientConfigOverrideMiddleware())

        @server.tool
        async def add_one(value: int) -> int:
            return value + 1

        with caplog.at_level(logging.INFO, logger="example-middleware"):
            async with Client(server) as client:
                result = await client.call_tool("add_one", {"value": 41})

        assert result.data == 42
        executing_logs = [
            r for r in caplog.records if "Executing tool: add_one" in r.message
        ]
        assert len(executing_logs) == 1
