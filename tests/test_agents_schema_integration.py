"""Agents Schema SQL and public tool behavior against the CI ClickHouse service."""

import json
import uuid

import clickhouse_connect
import pytest
from fastmcp import Client

from mcp_clickhouse import agents_schema, clients
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
        server_settings = admin.server_settings
        get_client_setting = admin.get_client_setting

        def query(self, *args, **kwargs):
            result = admin.query(*args, **kwargs)
            received.extend(result.result_rows)
            return result

    notes = _dbt_model_notes(ObservedClient(), {(data_db, "orders")})
    assert received == [("orders", data_db, "\u00e9" * MAX_DESCRIPTION_CHARS)]
    assert notes == [f"dbt model `{data_db}`.`orders`: " + "\u00e9" * MAX_DESCRIPTION_CHARS]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_in_settings", [False, True])
async def test_temporary_table_does_not_inherit_permanent_table_context(
    published_context, monkeypatch, session_in_settings
):
    _, data_db, _ = published_context
    session_id = uuid.uuid4().hex
    overrides = (
        {"settings": {"session_id": session_id}}
        if session_in_settings
        else {"session_id": session_id}
    )
    config = clients._resolve_client_config(overrides)
    session = clickhouse_connect.get_client(**config)
    try:
        session.command("CREATE TEMPORARY TABLE orders (id UInt64) ENGINE = Memory")
        session.command("INSERT INTO orders VALUES (7)")
        monkeypatch.setattr(clients, "_resolve_client_config", lambda *args: config)
        async with Client(mcp) as client:
            bare = await client.call_tool("run_query", {"query": "SELECT * FROM orders"})
            qualified = await client.call_tool(
                "run_query", {"query": f"SELECT * FROM {data_db}.orders FINAL"}
            )
        assert json.loads(bare.content[0].text) == {"columns": ["id"], "rows": [[7]]}
        payload = json.loads(qualified.content[0].text)
        assert payload["rows"] == [[1]]
        items = payload["agents_schema_context"]["items"]
        assert f"dbt model `{data_db}`.`orders`: " + "\u00e9" * MAX_DESCRIPTION_CHARS in items
        assert any("If the query does not already account for" in item for item in items)
    finally:
        try:
            session.command("DROP TEMPORARY TABLE IF EXISTS orders")
        finally:
            session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("readonly,metadata_grant", [(1, True), (1, False), (2, True)])
async def test_restricted_user_context_respects_metadata_grants(
    published_context, monkeypatch, readonly, metadata_grant
):
    admin, data_db, metadata_db = published_context
    username = "agents_reader_" + uuid.uuid4().hex
    password = uuid.uuid4().hex
    created = False
    try:
        # Test-only admin provisioning; the MCP tool runs with SELECT grants only.
        admin.command(
            f"CREATE USER {username} IDENTIFIED BY '{password}' SETTINGS readonly = {readonly}"
        )
        created = True
        admin.command(f"GRANT SELECT ON {data_db}.orders TO {username}")
        if metadata_grant:
            admin.command(f"GRANT SELECT ON {metadata_db}.* TO {username}")
        # Warm the same owner's caches as the privileged account first. A later
        # restricted caller must not inherit its discovery hint or descriptions.
        async with Client(mcp) as client:
            privileged = await client.call_tool("run_query", {"query": "SELECT * FROM orders"})
        assert any(
            "dbt model" in item
            for item in json.loads(privileged.content[0].text)["agents_schema_context"]["items"]
        )
        config = clients._resolve_client_config({"username": username, "password": password})
        monkeypatch.setattr(clients, "_resolve_client_config", lambda *args: config)
        async with Client(mcp) as client:
            result = await client.call_tool("run_query", {"query": "SELECT * FROM orders"})
        payload = json.loads(result.content[0].text)
        assert payload["rows"] == [[1]]
        items = payload["agents_schema_context"]["items"]
        assert any("ReplacingMergeTree" in item for item in items)
        assert any("dbt model" in item for item in items) is metadata_grant
        assert any(f"{metadata_db}.ROOT" in item for item in items) is metadata_grant
    finally:
        _clickhouse_clients._clear_client_cache()
        if created:
            admin.command(f"DROP USER {username}")
