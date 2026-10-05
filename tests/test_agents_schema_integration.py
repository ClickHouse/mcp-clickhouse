"""Agents Schema SQL and public tool behavior against the CI ClickHouse service."""

import json
import uuid

import clickhouse_connect
import pytest
from fastmcp import Client

from mcp_clickhouse import agents_schema
from mcp_clickhouse.agents_schema import MAX_DESCRIPTION_CHARS, _AgentsSchema, _dbt_model_notes
from mcp_clickhouse.mcp_env import get_config
from mcp_clickhouse.mcp_server import _clickhouse_clients, _queries, mcp


@pytest.fixture
def published_context(monkeypatch):
    admin = clickhouse_connect.get_client(**get_config().get_client_config())
    data_db = "agents_data_" + uuid.uuid4().hex
    metadata_db = "agents_metadata_" + uuid.uuid4().hex
    created = []
    try:
        for database in (data_db, metadata_db):
            admin.command(f"CREATE DATABASE {database}")
            created.append(database)
        admin.command(
            f"CREATE TABLE {data_db}.orders (id UInt64) ENGINE = ReplacingMergeTree ORDER BY id"
        )
        admin.command(f"INSERT INTO {data_db}.orders VALUES (1)")
        admin.command(f"CREATE TABLE {data_db}.`orders-archive` (id UInt64) ENGINE = Memory")
        admin.command(f"INSERT INTO {data_db}.`orders-archive` VALUES (7)")
        admin.command(
            f"CREATE TABLE {metadata_db}.ROOT (provider String, key String, content String) "
            "ENGINE = Memory"
        )
        admin.command(
            f"CREATE TABLE {metadata_db}.DBT_MODEL "
            "(name String, schema_name String, description String) ENGINE = Memory"
        )
        admin.insert(
            f"{metadata_db}.DBT_MODEL",
            [("orders", data_db, "\u00e9" * 1000), ("orders-archive", data_db, "Archived orders.")],
            column_names=["name", "schema_name", "description"],
        )
        # Keep real SQL and MCP execution, but never modify an existing AGENTS
        # database. Unit tests also assert the canonical name when unpatched.
        monkeypatch.setattr(agents_schema, "AGENTS_DATABASE", metadata_db)
        monkeypatch.setattr(_queries, "agents_schema", _AgentsSchema())
        monkeypatch.setenv("CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY", "true")
        monkeypatch.setenv("CLICKHOUSE_DATABASE", data_db)
        monkeypatch.setenv("CLICKHOUSE_ALLOW_WRITE_ACCESS", "false")
        yield admin, data_db, metadata_db
    finally:
        _clickhouse_clients._clear_client_cache()
        try:
            for database in reversed(created):
                admin.command(f"DROP DATABASE {database}")
        finally:
            admin.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "SELECT 'FROM {db}.orders' AS message",
        "SELECT 1 -- FROM {db}.orders",
        "WITH orders AS (SELECT 7 AS id) SELECT * FROM orders",
        "SELECT * FROM (WITH orders AS (SELECT 7 AS id) SELECT * FROM orders)",
    ],
)
async def test_unrelated_references_do_not_attach_context(published_context, query):
    admin, data_db, _ = published_context
    sql = query.format(db=data_db)
    expected = admin.query(sql)
    async with Client(mcp) as client:
        result = await client.call_tool("run_query", {"query": sql})
    assert json.loads(result.content[0].text) == {
        "columns": list(expected.column_names),
        "rows": [list(row) for row in expected.result_rows],
    }


@pytest.mark.asyncio
async def test_quoted_table_receives_only_its_own_description(published_context):
    _, data_db, _ = published_context
    async with Client(mcp) as client:
        result = await client.call_tool(
            "run_query", {"query": f'SELECT * FROM "{data_db}" . "orders-archive"'}
        )
    payload = json.loads(result.content[0].text)
    assert payload["rows"] == [[7]]
    items = payload["agents_schema_context"]["items"]
    assert any("Archived orders." in item for item in items)
    assert not any("ReplacingMergeTree" in item for item in items)


@pytest.mark.asyncio
async def test_public_query_preserves_rows_and_adds_relevant_context(published_context):
    _, data_db, metadata_db = published_context
    async with Client(mcp) as client:
        result = await client.call_tool(
            "run_query",
            {
                "query": "SELECT id, toUInt64('18446744073709551615') AS big FROM orders "
                "WHERE id = {id:UInt32}",
                "params": {"id": 1},
            },
        )
    payload = json.loads(result.content[0].text)
    assert payload["columns"] == ["id", "big"]
    assert payload["rows"] == [[1, "18446744073709551615"]]
    items = payload["agents_schema_context"]["items"]
    assert f"dbt model `{data_db}`.`orders`: " + "\u00e9" * MAX_DESCRIPTION_CHARS in items
    assert any("ReplacingMergeTree" in item for item in items)
    assert any(f"{metadata_db}.ROOT" in item for item in items)


def test_description_is_truncated_before_transfer(published_context):
    admin, data_db, _ = published_context
    received = []

    class ObservedClient:
        def query(self, *args, **kwargs):
            result = admin.query(*args, **kwargs)
            received.extend(result.result_rows)
            return result

    notes = _dbt_model_notes(ObservedClient(), {(data_db, "orders")})
    assert received == [("orders", data_db, "\u00e9" * MAX_DESCRIPTION_CHARS)]
    assert notes == [f"dbt model `{data_db}`.`orders`: " + "\u00e9" * MAX_DESCRIPTION_CHARS]
