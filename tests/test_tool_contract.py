"""Observable tool contract: annotations, output schema, titles, server info."""

import importlib.metadata
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from mcp_clickhouse import mcp_env, mcp_server
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
        # The drop gate is a keyword guard, not a boundary, so write access is
        # destructive with or without it.
        (
            True,
            False,
            {
                "read_only_hint": False,
                "destructive_hint": True,
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
        mcp_server, "ClickHouseConfig", return_value=_config(allow_write_access, allow_drop)
    ):
        mcp_server._register_clickhouse_tools(server)

    tools = await _list_tools_by_name(server)
    assert set(tools) == {"list_databases", "list_tables", "run_query"}
    assert _annotations(tools["run_query"]) == expected
    assert _annotations(tools["list_databases"]) == _READ_ONLY_ANNOTATIONS
    assert _annotations(tools["list_tables"]) == _READ_ONLY_ANNOTATIONS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_write_access", "allow_drop", "read_only_hint", "destructive_hint"),
    [
        (None, None, True, False),
        ("true", None, False, True),
        ("true", "true", False, True),
        (None, "true", True, False),
    ],
)
async def test_run_query_annotations_are_read_from_the_environment(
    monkeypatch, allow_write_access, allow_drop, read_only_hint, destructive_hint
):
    """The gates reach the annotations through ClickHouseConfig, not a stand-in.

    Registration builds a throwaway config, so no singleton reset is needed and
    the cached get_config() instance is left alone.
    """
    monkeypatch.setenv("CLICKHOUSE_ENABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "secret")
    for name, value in (
        ("CLICKHOUSE_ALLOW_WRITE_ACCESS", allow_write_access),
        ("CLICKHOUSE_ALLOW_DROP", allow_drop),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    singleton_before = mcp_env._CONFIG_INSTANCE

    server = FastMCP("annotations-env-test")
    mcp_server._register_clickhouse_tools(server)

    assert mcp_env._CONFIG_INSTANCE is singleton_before
    tools = await _list_tools_by_name(server)
    annotations = _annotations(tools["run_query"])
    assert annotations["read_only_hint"] is read_only_hint
    assert annotations["destructive_hint"] is destructive_hint
    assert annotations["idempotent_hint"] is False
    assert annotations["open_world_hint"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_write_access", "allow_drop", "must_contain", "must_not_contain"),
    [
        (
            False,
            False,
            ["This server is read-only", "CLICKHOUSE_ALLOW_WRITE_ACCESS=true", "SETTINGS"],
            ["CLICKHOUSE_ALLOW_DROP", "allows DDL"],
        ),
        (
            True,
            False,
            ["This server allows DDL and DML", "CLICKHOUSE_ALLOW_DROP=true", "DROP, TRUNCATE"],
            ["read-only", "CLICKHOUSE_ALLOW_WRITE_ACCESS"],
        ),
        (
            True,
            True,
            ["destructive statements", "Nothing in the MCP server blocks them", "grants"],
            ["read-only", "CLICKHOUSE_ALLOW_DROP=true", "CLICKHOUSE_ALLOW_WRITE_ACCESS"],
        ),
    ],
)
async def test_run_query_description_states_the_configured_mode(
    allow_write_access, allow_drop, must_contain, must_not_contain
):
    """A client can tell a read-only instance from a writable one without a write."""
    with patch.object(
        mcp_server, "ClickHouseConfig", return_value=_config(allow_write_access, allow_drop)
    ):
        server = FastMCP("description-test")
        mcp_server._register_clickhouse_tools(server)

    tools = await _list_tools_by_name(server)
    description = tools["run_query"].description
    assert description.startswith("Execute SQL queries in ClickHouse.")
    for text in must_contain:
        assert text in description, text
    for text in must_not_contain:
        assert text not in description, text
    assert "9007199254740991" in description
    assert tools["run_query"].input_schema["properties"]["query"]["description"]


@pytest.mark.asyncio
async def test_registration_survives_incomplete_config_and_advertises_read_only():
    """Import never failed on missing connection variables; keep it that way."""
    server = FastMCP("annotations-test")
    with patch.object(
        mcp_server,
        "ClickHouseConfig",
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
    assert "This server is read-only" in tools["run_query"].description


_PERSISTENT_CHDB_ANNOTATIONS = {
    "read_only_hint": False,
    "destructive_hint": True,
    "idempotent_hint": False,
    "open_world_hint": True,
}


async def _register_chdb_tool_on_fresh_server(monkeypatch, data_path):
    monkeypatch.setenv("CHDB_ENABLED", "true")
    if data_path is None:
        monkeypatch.delenv("CHDB_DATA_PATH", raising=False)
    else:
        monkeypatch.setenv("CHDB_DATA_PATH", data_path)
    with (
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
    return tools["run_chdb_select_query"]


@pytest.mark.asyncio
@pytest.mark.parametrize("data_path", [None, ":memory:"])
async def test_chdb_tool_in_memory_is_read_only_with_no_output_schema(monkeypatch, data_path):
    tool = await _register_chdb_tool_on_fresh_server(monkeypatch, data_path)

    assert _annotations(tool) == _READ_ONLY_ANNOTATIONS
    assert tool.output_schema is None
    assert tool.title == "Run Chdb Select Query"
    assert "in-memory" in tool.description
    assert "raise a tool error" in tool.description
    assert tool.input_schema["properties"]["query"]["description"]


@pytest.mark.asyncio
async def test_chdb_tool_on_a_filesystem_data_path_is_writable_and_destructive(
    monkeypatch, tmp_path
):
    """chDB runs any SQL; with a persistent data path a CREATE or DROP is durable."""
    tool = await _register_chdb_tool_on_fresh_server(monkeypatch, str(tmp_path / "chdb"))

    assert _annotations(tool) == _PERSISTENT_CHDB_ANNOTATIONS
    assert "persists data on disk" in tool.description
    assert str(tmp_path) not in tool.description


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
