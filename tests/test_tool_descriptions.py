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


@pytest.mark.asyncio
async def test_run_query_exposes_optional_params():
    async with Client(mcp) as client:
        tools = await client.list_tools()

    tool = next(tool for tool in tools if tool.name == "run_query")
    assert tool.input_schema["required"] == ["query"]
    params_schema = tool.input_schema["properties"]["params"]
    assert params_schema["default"] is None
    assert {variant["type"] for variant in params_schema["anyOf"]} == {"object", "null"}
    assert "{name:Type}" in tool.description
    assert "Array(Float32)" in tool.description


@pytest.mark.asyncio
async def test_run_query_description_warns_about_format_and_settings():
    """The description tells clients about two clauses that always fail through run_query."""
    async with Client(mcp) as client:
        tools = await client.list_tools()

    description = next(tool for tool in tools if tool.name == "run_query").description

    assert "Do not end a query with a FORMAT clause" in description
    assert "FORMAT Native" in description
    assert "top-level SETTINGS clause" in description
    assert "code 164" in description
