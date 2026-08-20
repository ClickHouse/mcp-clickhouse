"""End-to-end tests for the chDB-only introspection tools.

These drive the tools through a real FastMCP server (via an in-memory FastMCP
client) backed by a real in-process chDB session, so the full path is exercised:
tool dispatch -> async wrapper -> thread-pool execution -> chDB -> truncation.

Skipped when chDB is not installed (the optional ``[chdb]`` extra).
"""

import concurrent.futures
import json

import pytest

chdb_session = pytest.importorskip("chdb.session")
from fastmcp import Client, FastMCP  # noqa: E402

from mcp_clickhouse.chdb_tools import register_chdb_only_tools  # noqa: E402

pytestmark = pytest.mark.asyncio


# Module-scoped: chDB is one-session-per-process, so the session and its seeded
# data are created once and reused across the module's tests. Setup is
# idempotent so re-runs in the same process do not fail on existing objects.
@pytest.fixture(scope="module")
def chdb_client():
    """A real in-process chDB session seeded with a demo database/table."""
    session = chdb_session.Session()
    session.query("CREATE DATABASE IF NOT EXISTS demo", "TabSeparated")
    session.query("DROP TABLE IF EXISTS demo.t", "TabSeparated")
    session.query("CREATE TABLE demo.t (id Int32, name String) ENGINE = Memory", "TabSeparated")
    session.query("INSERT INTO demo.t VALUES (1, 'a'), (2, 'b'), (3, 'c')", "TabSeparated")
    # Mirror the server init flow: the session is locked to readonly=2 before it
    # is handed to ChDBTool, which verifies (and never mutates) the readonly
    # mode of an externally provided session.
    session.query("SET readonly=2", "TabSeparated")
    yield session
    session.close()


@pytest.fixture(scope="module")
def mcp_with_tools(chdb_client):
    """A FastMCP server with the chDB-only tools registered against the session."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    mcp = FastMCP(name="chdb-test")
    register_chdb_only_tools(
        mcp,
        max_result_bytes=1024 * 1024,
        create_client=lambda: chdb_client,
        query_executor=executor,
        query_timeout=lambda: 30,
    )
    yield mcp
    executor.shutdown(wait=True)


async def test_allow_write_does_not_clobber_session():
    # Regression: registering with allow_write=True must NOT put the shared
    # session under readonly=2 — that clobber is irreversible and would also
    # strip write access from the legacy run_chdb_select_query on the same session.
    session = chdb_session.Session()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        register_chdb_only_tools(
            FastMCP(name="chdb-write-test"),
            max_result_bytes=1024 * 1024,
            create_client=lambda: session,
            query_executor=executor,
            query_timeout=lambda: 30,
            allow_write=True,
        )
        # the session must still accept a write
        session.query("CREATE TABLE wprobe (a Int32) ENGINE = Memory", "TabSeparated")
        session.query("INSERT INTO wprobe VALUES (1)", "TabSeparated")
        assert "1" in str(session.query("SELECT count() FROM wprobe", "CSV"))
    finally:
        executor.shutdown(wait=True)
        session.close()


async def test_all_five_tools_are_registered(mcp_with_tools):
    tools = await mcp_with_tools.get_tools()
    assert {
        "list_databases",
        "list_tables",
        "describe_table",
        "get_sample_data",
        "list_functions",
    } <= set(tools)


async def test_list_databases(mcp_with_tools):
    async with Client(mcp_with_tools) as client:
        result = await client.call_tool("list_databases", {})
    assert "demo" in result.data


async def test_list_tables(mcp_with_tools):
    async with Client(mcp_with_tools) as client:
        result = await client.call_tool("list_tables", {"database": "demo"})
    assert "t" in result.data


async def test_describe_table(mcp_with_tools):
    async with Client(mcp_with_tools) as client:
        result = await client.call_tool("describe_table", {"database": "demo", "table": "t"})
    assert "id" in result.data and "Int32" in result.data
    assert "name" in result.data and "String" in result.data


async def test_get_sample_data_respects_limit(mcp_with_tools):
    async with Client(mcp_with_tools) as client:
        result = await client.call_tool(
            "get_sample_data", {"database": "demo", "table": "t", "limit": 2}
        )
    # rows are returned as a JSON array; the limit bounds the returned count
    rows = json.loads(result.data)
    assert len(rows) == 2


async def test_list_functions_filter(mcp_with_tools):
    async with Client(mcp_with_tools) as client:
        result = await client.call_tool("list_functions", {"pattern": "cosine"})
    assert "cosineDistance" in result.data


async def test_malicious_database_name_is_inert(mcp_with_tools):
    async with Client(mcp_with_tools) as client:
        # ChDBTool binds `database` as a value, so a "; DROP ..." name never
        # reaches SQL as code: it matches no database (empty result) and cannot
        # drop anything.
        result = await client.call_tool(
            "list_tables", {"database": "demo; DROP TABLE demo.t"}
        )
        assert json.loads(result.data) == []
        # the real table is untouched
        result2 = await client.call_tool("list_tables", {"database": "demo"})
        assert "t" in result2.data


async def test_bad_query_raises_tool_error(mcp_with_tools):
    # errors are raised (like the ClickHouse-server tools), not returned as strings
    async with Client(mcp_with_tools) as client:
        with pytest.raises(Exception):
            await client.call_tool(
                "describe_table", {"database": "demo", "table": "nonexistent"}
            )
