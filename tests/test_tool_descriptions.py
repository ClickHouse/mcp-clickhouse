"""Tests for what ClickHouse tool descriptions advertise."""

import pytest
from fastmcp import Client

from mcp_clickhouse.mcp_server import mcp


@pytest.mark.asyncio
async def test_run_query_description_names_describe_and_explain_estimate():
    """Both statements stay in the description an MCP client reads."""
    async with Client(mcp) as client:
        tools = await client.list_tools()

    run_query_tool = next(tool for tool in tools if tool.name == "run_query")

    assert "DESCRIBE (<query>)" in run_query_tool.description
    assert "EXPLAIN ESTIMATE <query>" in run_query_tool.description
