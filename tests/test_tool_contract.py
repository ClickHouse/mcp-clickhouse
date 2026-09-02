"""Observable tool contract: annotations, output schema, titles, server info."""

import importlib.metadata
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from mcp_clickhouse import mcp_server
from mcp_clickhouse.mcp_server import MCP_SERVER_WEBSITE_URL, mcp

_READ_ONLY_ANNOTATIONS = {
    "read_only_hint": True,
    "destructive_hint": False,
    "idempotent_hint": True,
    "open_world_hint": True,
}


def _config(allow_write_access: bool, allow_drop: bool) -> SimpleNamespace:
    return SimpleNamespace(allow_write_access=allow_write_access, allow_drop=allow_drop)


async def _list_tools_by_name(server: FastMCP) -> dict:
    async with Client(server) as client:
        return {tool.name: tool for tool in await client.list_tools()}


def _annotations(tool) -> dict:
    return tool.annotations.model_dump(exclude_none=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_write_access", "allow_drop", "expected"),
    [
        (
            False,
            False,
            {
                "read_only_hint": True,
                "destructive_hint": False,
                "idempotent_hint": False,
                "open_world_hint": True,
            },
        ),
        (
            True,
            False,
            {
                "read_only_hint": False,
                "destructive_hint": False,
                "idempotent_hint": False,
                "open_world_hint": True,
            },
        ),
        (
            True,
            True,
            {
                "read_only_hint": False,
                "destructive_hint": True,
                "idempotent_hint": False,
                "open_world_hint": True,
            },
        ),
        # CLICKHOUSE_ALLOW_DROP without write access changes nothing.
        (
            False,
            True,
            {
                "read_only_hint": True,
                "destructive_hint": False,
                "idempotent_hint": False,
                "open_world_hint": True,
            },
        ),
    ],
)
async def test_run_query_annotations_track_write_and_drop_gates(
    allow_write_access, allow_drop, expected
):
    server = FastMCP("annotations-test")
    with patch.object(
        mcp_server, "get_config", return_value=_config(allow_write_access, allow_drop)
    ):
        mcp_server._register_clickhouse_tools(server)

    tools = await _list_tools_by_name(server)
    assert set(tools) == {"list_databases", "list_tables", "run_query"}
    assert _annotations(tools["run_query"]) == expected
    assert _annotations(tools["list_databases"]) == _READ_ONLY_ANNOTATIONS
    assert _annotations(tools["list_tables"]) == _READ_ONLY_ANNOTATIONS


@pytest.mark.asyncio
async def test_registration_survives_incomplete_config_and_advertises_read_only():
    """Import never failed on missing connection variables; keep it that way."""
    server = FastMCP("annotations-test")
    with patch.object(
        mcp_server,
        "get_config",
        side_effect=ValueError("Missing required environment variables: CLICKHOUSE_HOST"),
    ):
        mcp_server._register_clickhouse_tools(server)

    tools = await _list_tools_by_name(server)
    assert _annotations(tools["run_query"]) == {
        "read_only_hint": True,
        "destructive_hint": False,
        "idempotent_hint": False,
        "open_world_hint": True,
    }


@pytest.mark.asyncio
async def test_chdb_tool_is_registered_with_read_only_annotations_and_no_output_schema():
    with (
        patch.dict("os.environ", {"CHDB_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_init_chdb_client", return_value=MagicMock()),
        patch.object(mcp_server, "_chdb_client", None),
        patch.object(mcp_server.mcp, "add_tool") as add_tool,
        patch.object(mcp_server.mcp, "add_prompt"),
        patch.object(mcp_server.atexit, "register"),
    ):
        mcp_server._register_chdb_tools()

    (chdb_tool,) = add_tool.call_args.args
    server = FastMCP("chdb-annotations-test")
    server.add_tool(chdb_tool)
    tools = await _list_tools_by_name(server)
    assert set(tools) == {"run_chdb_select_query"}
    assert _annotations(tools["run_chdb_select_query"]) == _READ_ONLY_ANNOTATIONS
    assert tools["run_chdb_select_query"].output_schema is None
    assert tools["run_chdb_select_query"].title == "Run Chdb Select Query"


@pytest.mark.asyncio
async def test_registered_tools_have_no_output_schema_and_derived_titles():
    tools = await _list_tools_by_name(mcp)
    assert {"list_databases", "list_tables", "run_query"} <= set(tools)
    for tool in tools.values():
        assert tool.output_schema is None, tool.name
    assert tools["list_databases"].title == "List Databases"
    assert tools["list_tables"].title == "List Tables"
    assert tools["run_query"].title == "Run Query"


@pytest.mark.asyncio
async def test_tool_results_are_plain_json_text_without_structured_content():
    async with Client(mcp) as client:
        result = await client.call_tool("list_databases", {})

    assert result.structured_content is None
    assert result.content[0].type == "text"
    assert result.content[0].text.startswith("[")


def test_server_version_matches_installed_package():
    assert mcp.version == importlib.metadata.version("mcp-clickhouse")
    assert mcp.website_url == MCP_SERVER_WEBSITE_URL == "https://github.com/ClickHouse/mcp-clickhouse"


@pytest.mark.asyncio
async def test_initialize_reports_package_version_and_website():
    async with Client(mcp) as client:
        server_info = client.server_info

    assert server_info.name == "mcp-clickhouse"
    assert server_info.version == importlib.metadata.version("mcp-clickhouse")
    assert server_info.website_url == MCP_SERVER_WEBSITE_URL


def test_package_version_returns_none_when_not_installed():
    with patch.object(
        mcp_server.importlib.metadata,
        "version",
        side_effect=importlib.metadata.PackageNotFoundError("mcp-clickhouse"),
    ):
        assert mcp_server._package_version() is None


@pytest.mark.parametrize("page_size", [0, -1])
def test_sync_list_tables_rejects_non_positive_page_size_directly(page_size):
    """The exported sync helper bypasses pydantic, so the guard must hold there."""
    with patch.object(mcp_server, "_acquire_clickhouse_client") as acquire:
        with pytest.raises(ToolError, match="page_size must be greater than 0"):
            mcp_server.list_tables("system", page_size=page_size)
    acquire.assert_not_called()
