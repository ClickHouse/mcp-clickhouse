"""Integration tests for the Postgres tools through the MCP boundary.

These need a reachable Postgres server and POSTGRES_* connection variables set
before mcp_server is imported, for example from test-services/docker-compose.yaml.
"""

import asyncio
import json
import time
import uuid

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_clickhouse import mcp_server
from mcp_clickhouse.mcp_env import get_postgres_config

pytestmark = pytest.mark.skipif(
    mcp_server._postgres_backend.psycopg is None,
    reason="requires POSTGRES_ENABLED=true, the postgres extra, and a Postgres server",
)


def _admin_connect():
    psycopg = mcp_server._postgres_backend.psycopg
    return psycopg.connect(**get_postgres_config().get_connect_kwargs(), autocommit=True)


@pytest.fixture
def schema():
    """Create a uniquely named schema with fixtures and drop it afterwards."""
    name = f"mcp_test_{uuid.uuid4().hex[:12]}"
    with _admin_connect() as conn:
        conn.execute(f"CREATE SCHEMA {name}")
        conn.execute(
            f"""
            CREATE TABLE {name}.accounts (
                id bigint GENERATED ALWAYS AS IDENTITY,
                region text NOT NULL DEFAULT 'eu',
                balance numeric(12, 2),
                PRIMARY KEY (region, id)
            )
            """
        )
        conn.execute(f"COMMENT ON TABLE {name}.accounts IS 'customer accounts'")
        conn.execute(f"COMMENT ON COLUMN {name}.accounts.balance IS 'in cents'")
        conn.execute(f"INSERT INTO {name}.accounts (region, balance) VALUES ('us', 10.50)")
        conn.execute(f"CREATE VIEW {name}.big_accounts AS SELECT * FROM {name}.accounts")
        conn.execute(
            f"CREATE TABLE {name}.events (day date NOT NULL) PARTITION BY RANGE (day)"
        )
        conn.execute(
            f"CREATE TABLE {name}.events_2026 PARTITION OF {name}.events "
            "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')"
        )
    try:
        yield name
    finally:
        with _admin_connect() as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


@pytest.fixture
def write_access(monkeypatch):
    monkeypatch.setenv("POSTGRES_ALLOW_WRITE_ACCESS", "true")
    monkeypatch.delenv("POSTGRES_ALLOW_DROP", raising=False)


async def _call(tool: str, **arguments):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool(tool, arguments)
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_tools_are_listed_with_expected_schemas():
    async with Client(mcp_server.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert tools["run_postgres_query"].input_schema["required"] == ["query"]
    assert "READ ONLY" in tools["run_postgres_query"].description
    assert tools["list_postgres_tables"].input_schema["required"] == ["schema"]
    assert "list_postgres_schemas" in tools


@pytest.mark.asyncio
async def test_run_postgres_query_returns_columns_and_rows():
    result = await _call(
        "run_postgres_query",
        query=(
            "SELECT 1 AS small, 9007199254740993::int8 AS big, 1.50::numeric AS amount, "
            "'{\"a\": [1, 2]}'::jsonb AS doc, NULL::text AS nothing, 'x%' AS pct"
        ),
    )

    assert result == {
        "columns": ["small", "big", "amount", "doc", "nothing", "pct"],
        "rows": [[1, "9007199254740993", "1.50", {"a": [1, 2]}, None, "x%"]],
    }


@pytest.mark.asyncio
async def test_run_postgres_query_rejects_writes_by_default(schema):
    with pytest.raises(ToolError, match="read-only transaction"):
        await _call("run_postgres_query", query=f"INSERT INTO {schema}.accounts (region) VALUES ('x')")

    rows = await _call("run_postgres_query", query=f"SELECT count(*) FROM {schema}.accounts")
    assert rows["rows"] == [[1]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1; COMMIT; CREATE TABLE {schema}.escaped (a int)",
        "SELECT 1; SET default_transaction_read_only = off",
    ],
)
async def test_run_postgres_query_cannot_escape_read_only_transaction(schema, query):
    with pytest.raises(ToolError, match="multiple commands"):
        await _call("run_postgres_query", query=query.format(schema=schema))

    with pytest.raises(ToolError, match="read-only transaction"):
        await _call("run_postgres_query", query=f"CREATE TABLE {schema}.escaped (a int)")


@pytest.mark.asyncio
async def test_run_postgres_query_cannot_switch_transaction_to_read_write():
    with pytest.raises(ToolError, match="read-write mode must be set before any query"):
        await _call("run_postgres_query", query="SET TRANSACTION READ WRITE")


@pytest.mark.asyncio
async def test_each_call_uses_a_new_connection():
    first = await _call("run_postgres_query", query="SELECT pg_backend_pid()")
    second = await _call("run_postgres_query", query="SELECT pg_backend_pid()")

    assert first["rows"] != second["rows"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["COMMIT", "BEGIN", "PREPARE TRANSACTION 'mcp_test'"])
async def test_transaction_control_is_rejected_before_reaching_postgres(query):
    with pytest.raises(ToolError, match="Transaction control statements"):
        await _call("run_postgres_query", query=query)

    with _admin_connect() as conn:
        prepared = conn.execute(
            "SELECT count(*) FROM pg_prepared_xacts WHERE gid = 'mcp_test'"
        ).fetchone()
    assert prepared == (0,)


@pytest.mark.asyncio
async def test_cancelled_write_is_rolled_back(schema, write_access, monkeypatch):
    backend = mcp_server._postgres_backend
    real_begin = backend._begin
    states = []

    def begin_then_cancel(cursor):
        real_begin(cursor)
        # Simulate a timeout that lands before the statement reaches the server.
        states[-1].cancelled = True

    real_execute = backend.execute_query

    def recording_execute(query, state=None):
        states.append(state)
        return real_execute(query, state)

    monkeypatch.setattr(backend, "_begin", begin_then_cancel)
    monkeypatch.setattr(backend, "execute_query", recording_execute)
    with pytest.raises(ToolError, match="rolled back"):
        await _call(
            "run_postgres_query",
            query=f"INSERT INTO {schema}.accounts (region) VALUES ('late')",
        )

    with _admin_connect() as conn:
        count = conn.execute(
            f"SELECT count(*) FROM {schema}.accounts WHERE region = 'late'"
        ).fetchone()
    assert count == (0,)


@pytest.mark.asyncio
async def test_write_access_commits_and_drop_needs_second_opt_in(schema, write_access, monkeypatch):
    created = await _call("run_postgres_query", query=f"CREATE TABLE {schema}.notes (body text)")
    assert created == {"columns": [], "rows": []}

    inserted = await _call(
        "run_postgres_query",
        query=f"INSERT INTO {schema}.notes VALUES ('hello') RETURNING body",
    )
    assert inserted["rows"] == [["hello"]]

    with pytest.raises(ToolError, match="POSTGRES_ALLOW_DROP=true"):
        await _call("run_postgres_query", query=f"DROP TABLE {schema}.notes")

    monkeypatch.setenv("POSTGRES_ALLOW_DROP", "true")
    await _call("run_postgres_query", query=f"DROP TABLE {schema}.notes")

    with _admin_connect() as conn:
        assert conn.execute("SELECT to_regclass(%s)", (f"{schema}.notes",)).fetchone() == (None,)


@pytest.mark.asyncio
async def test_failed_statement_reports_postgres_error():
    with pytest.raises(ToolError, match='relation "no_such_table_' ):
        await _call("run_postgres_query", query=f"SELECT * FROM no_such_table_{uuid.uuid4().hex}")


@pytest.mark.asyncio
async def test_timeout_cancels_statement_on_server(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_MCP_QUERY_TIMEOUT", "1")
    backend = mcp_server._postgres_backend

    def begin_without_server_timeout(cursor):
        # Leave statement_timeout far above the tool timeout so only the
        # client-side cancel can stop the statement.
        cursor.execute("SELECT set_config('statement_timeout', '60s', true)")

    monkeypatch.setattr(backend, "_begin", begin_without_server_timeout)
    marker = f"mcp_timeout_{uuid.uuid4().hex}"
    query = f"SELECT pg_sleep(30) AS {marker}"

    started = time.monotonic()
    with pytest.raises(ToolError, match="timed out after 1 seconds"):
        await _call("run_postgres_query", query=query)
    assert time.monotonic() - started < 10

    with _admin_connect() as conn:
        for _ in range(50):
            running = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE query LIKE %s AND state = 'active' AND pid <> pg_backend_pid()",
                (f"%{marker}%",),
            ).fetchone()[0]
            if running == 0:
                break
            time.sleep(0.1)
    assert running == 0


@pytest.mark.asyncio
async def test_slow_query_does_not_block_other_calls():
    async with Client(mcp_server.mcp) as client:
        slow = asyncio.create_task(
            client.call_tool("run_postgres_query", {"query": "SELECT pg_sleep(1.5)"})
        )
        await asyncio.sleep(0.1)
        started = time.monotonic()
        fast = await client.call_tool("run_postgres_query", {"query": "SELECT 1"})
        elapsed = time.monotonic() - started
        assert not slow.done()
        await slow

    assert json.loads(fast.content[0].text)["rows"] == [[1]]
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_list_postgres_schemas_excludes_system_schemas(schema):
    schemas = await _call("list_postgres_schemas")

    assert schema in schemas
    assert "public" in schemas
    assert "pg_catalog" not in schemas
    assert "information_schema" not in schemas


@pytest.mark.asyncio
async def test_list_postgres_tables_describes_relations(schema):
    result = await _call("list_postgres_tables", schema=schema)

    assert result["total_tables"] == 3
    assert result["next_page_token"] is None
    tables = {table["name"]: table for table in result["tables"]}
    # Partitions are folded into their partitioned parent.
    assert set(tables) == {"accounts", "big_accounts", "events"}
    assert tables["events"]["kind"] == "partitioned table"
    assert tables["big_accounts"]["kind"] == "view"

    accounts = tables["accounts"]
    assert accounts["kind"] == "table"
    assert accounts["schema"] == schema
    assert accounts["comment"] == "customer accounts"
    assert accounts["primary_key"] == ["region", "id"]
    columns = {column["name"]: column for column in accounts["columns"]}
    assert [column["name"] for column in accounts["columns"]] == ["id", "region", "balance"]
    assert columns["id"]["default_kind"] == "identity"
    assert columns["id"]["nullable"] is False
    assert columns["region"]["default_kind"] == "default"
    assert columns["region"]["default_expression"] == "'eu'::text"
    assert columns["balance"]["column_type"] == "numeric(12,2)"
    assert columns["balance"]["comment"] == "in cents"


@pytest.mark.asyncio
async def test_list_postgres_tables_filters_and_omits_columns(schema):
    result = await _call(
        "list_postgres_tables",
        schema=schema,
        like="%accounts",
        not_like="big%",
        include_detailed_columns=False,
    )

    assert result["total_tables"] == 1
    assert [table["name"] for table in result["tables"]] == ["accounts"]
    assert result["tables"][0]["columns"] == []


@pytest.mark.asyncio
async def test_list_postgres_tables_paginates(schema):
    names = []
    page_token = None
    for _ in range(5):
        arguments = {"schema": schema, "page_size": 1}
        if page_token:
            arguments["page_token"] = page_token
        page = await _call("list_postgres_tables", **arguments)
        assert page["total_tables"] == 3
        names.extend(table["name"] for table in page["tables"])
        page_token = page["next_page_token"]
        if page_token is None:
            break

    assert names == ["accounts", "big_accounts", "events"]


@pytest.mark.asyncio
async def test_list_postgres_tables_rejects_token_for_other_filters(schema):
    page = await _call("list_postgres_tables", schema=schema, page_size=1)

    with pytest.raises(ToolError, match="different schema or filters"):
        await _call(
            "list_postgres_tables",
            schema=schema,
            like="a%",
            page_token=page["next_page_token"],
        )
    with pytest.raises(ToolError, match="Invalid page_token"):
        await _call("list_postgres_tables", schema=schema, page_token="not-a-token")


@pytest.mark.asyncio
async def test_list_postgres_tables_treats_schema_as_a_value_not_sql():
    result = await _call("list_postgres_tables", schema="public' OR '1'='1")

    assert result == {"tables": [], "next_page_token": None, "total_tables": 0}
