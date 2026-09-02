"""Tests for list_tables input validation."""

from unittest.mock import patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_clickhouse.mcp_server import mcp


@pytest.mark.parametrize("page_size", [0, -1])
@pytest.mark.asyncio
async def test_list_tables_rejects_non_positive_page_size(page_size):
    """Reject page sizes that cannot produce a valid page through MCP."""
    with patch("mcp_clickhouse.mcp_server._acquire_clickhouse_client") as acquire_client:
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="greater than 0"):
                await client.call_tool(
                    "list_tables",
                    {"database": "database", "page_size": page_size},
                )

    acquire_client.assert_not_called()


@pytest.mark.asyncio
async def test_list_tables_page_size_schema_requires_positive_value():
    """Advertise the positive page size requirement to MCP clients."""
    async with Client(mcp) as client:
        tools = await client.list_tools()

    list_tables_tool = next(tool for tool in tools if tool.name == "list_tables")
    page_size_schema = list_tables_tool.input_schema["properties"]["page_size"]

    assert page_size_schema["exclusiveMinimum"] == 0


@pytest.mark.asyncio
async def test_list_tables_description_states_response_shape():
    """The explicit description keeps the response shape that the docstring Returns block carried."""
    async with Client(mcp) as client:
        tools = await client.list_tools()

    list_tables_tool = next(tool for tool in tools if tool.name == "list_tables")

    assert "next_page_token" in list_tables_tool.description
    assert "total_tables" in list_tables_tool.description
    assert "tables" in list_tables_tool.description
    properties = list_tables_tool.input_schema["properties"]
    assert properties["database"]["description"] == "The database to list tables from"
    assert list_tables_tool.input_schema["additionalProperties"] is False
