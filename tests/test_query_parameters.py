"""Parameter binding through the existing ClickHouse query tool."""

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from clickhouse_connect.driver.binding import external_bind_re
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_clickhouse.clients import (
    _ClientCacheEntry,
)
from mcp_clickhouse.mcp_server import (
    create_clickhouse_client,
    mcp,
    run_query,
    run_query_async,
)
from mcp_clickhouse.queries import _has_unclosed_placeholder


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "type_name,value,expected",
    [
        ("UInt32", 13, 13),
        (" UInt32 ", 13, 13),
        ("Float64", 1.25, 1.25),
        ("Bool", True, True),
        (
            "String",
            "O'Reilly\\notes\n\t'); DROP TABLE example; --",
            "O'Reilly\\notes\n\t'); DROP TABLE example; --",
        ),
        ("Nullable(UInt32)", None, None),
        ("Array(UInt32)", [13, 79], [13, 79]),
        ("Array(String)", ["a'b", "c\\d", "e\nf"], ["a'b", "c\\d", "e\nf"]),
        ("Array(Float32)", [], []),
        ("UInt64", "18446744073709551615", "18446744073709551615"),
        ("UInt256", str((1 << 256) - 1), str((1 << 256) - 1)),
        ("Int128", str(-(1 << 127)), str(-(1 << 127))),
        ("Date", "2026-09-16", "2026-09-16"),
        ("DateTime('UTC')", "2026-09-16 12:34:56", "2026-09-16 12:34:56"),
        ("DateTime( 'UTC' )", "2026-09-16 12:34:56", "2026-09-16 12:34:56"),
        ("Decimal(18, 4)", "13.2500", "13.2500"),
    ],
)
async def test_registered_query_binds_json_values(type_name, value, expected):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "run_query",
            {"query": f"SELECT {{value:{type_name}}} AS value", "params": {"value": value}},
        )

    assert isinstance(result.content[0].text, str)
    response = json.loads(result.content[0].text)
    assert response == {"columns": ["value"], "rows": [[expected]]}
    assert type(response["rows"][0][0]) is type(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,columns,row",
    [
        ("SELECT {id:UInt32} AS id, '{' AS literal", ["id", "literal"], [13, "{"]),
        ("SELECT {id:UInt32} AS id -- {", ["id"], [13]),
        ("SELECT {id:UInt32} AS id /* { */", ["id"], [13]),
        ("SELECT {id:UInt32} AS id /* { ordinary: text */", ["id"], [13]),
        ("SELECT {id:UInt32} AS id /* {a:b */", ["id"], [13]),
        ("SELECT {id:UInt32} AS id, 'text {a:b' AS lit", ["id", "lit"], [13, "text {a:b"]),
        ("SELECT {id:UInt32} AS id -- {a:b", ["id"], [13]),
    ],
)
async def test_registered_query_accepts_literal_and_comment_braces(query, columns, row):
    async with Client(mcp) as client:
        result = await client.call_tool("run_query", {"query": query, "params": {"id": 13}})

    assert json.loads(result.content[0].text) == {"columns": columns, "rows": [row]}


@pytest.mark.asyncio
async def test_registered_query_accepts_long_enum_placeholder():
    labels = ", ".join(f"'label_{index:02}' = {index}" for index in range(40))
    placeholder = "{value:Enum8(" + labels + ")}"
    assert len(placeholder) > 512

    async with Client(mcp) as client:
        result = await client.call_tool(
            "run_query",
            {"query": f"SELECT {placeholder} AS value", "params": {"value": "label_13"}},
        )

    assert json.loads(result.content[0].text) == {"columns": ["value"], "rows": [["label_13"]]}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["_value", "9value", "$value", "value$", "value$part"])
async def test_registered_query_accepts_driver_supported_parameter_names(name):
    placeholder = "{" + name + ":UInt32}"
    if external_bind_re.fullmatch(placeholder) is None:
        pytest.skip("Installed driver does not bind this parameter name")

    async with Client(mcp) as client:
        result = await client.call_tool(
            "run_query", {"query": f"SELECT {placeholder} AS value", "params": {name: 13}}
        )

    assert json.loads(result.content[0].text) == {"columns": ["value"], "rows": [[13]]}


@pytest.mark.asyncio
async def test_registered_query_binds_1024_element_vector():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "run_query",
            {
                "query": (
                    "SELECT length({vector:Array(Float32)}) AS n, "
                    "arraySum({vector:Array(Float32)}) AS total"
                ),
                "params": {"vector": [0.25] * 1024},
            },
        )

    assert json.loads(result.content[0].text) == {"columns": ["n", "total"], "rows": [[1024, 256.0]]}


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"params": None}, {"params": {}}])
async def test_registered_query_accepts_omitted_or_empty_params(arguments):
    async with Client(mcp) as client:
        result = await client.call_tool("run_query", {"query": "SELECT 1 AS n", **arguments})

    assert json.loads(result.content[0].text) == {"columns": ["n"], "rows": [[1]]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params,error",
    [
        ({}, "UNKNOWN_QUERY_PARAMETER"),
        ({"other": 13}, "UNKNOWN_QUERY_PARAMETER"),
        ({"value": "invalid-number"}, "BAD_QUERY_PARAMETER"),
    ],
)
async def test_registered_query_reports_parameter_errors(params, error):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "run_query",
            {"query": "SELECT {value:UInt32}", "params": params},
            raise_on_error=False,
        )

    assert result.is_error
    assert "Query execution failed" in result.content[0].text
    assert error in result.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [[], [13], "invalid", 13, True])
async def test_registered_query_rejects_non_object_params(params):
    with patch("mcp_clickhouse.mcp_server._queries.clients._acquire_clickhouse_client") as acquire:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "run_query", {"query": "SELECT 1", "params": params}, raise_on_error=False
            )

    assert result.is_error
    assert "params" in result.content[0].text
    acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,params",
    [
        ("%(d)c%(r)c%(o)c%(p)c TABLE example", {"d": 68, "r": 82, "o": 79, "p": 80}),
        ("SELECT %(value)s", {"value": 13}),
        ("SELECT {value:UInt32}, $raw$", {"value": 13, "$raw$": "raw-sql-marker"}),
    ],
)
async def test_registered_query_rejects_client_side_binding(monkeypatch, query, params):
    monkeypatch.setenv("CLICKHOUSE_ALLOW_WRITE_ACCESS", "true")
    monkeypatch.setenv("CLICKHOUSE_ALLOW_DROP", "false")
    fake_client = SimpleNamespace(
        server_settings={},
        query=MagicMock(return_value=SimpleNamespace(column_names=[], result_rows=[])),
    )
    entry = _ClientCacheEntry(fake_client, 0, active_users=1)
    with patch("mcp_clickhouse.mcp_server._queries.clients._acquire_clickhouse_client", return_value=entry) as acquire:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "run_query", {"query": query, "params": params}, raise_on_error=False
            )

    assert result.is_error
    assert "{name:Type}" in result.content[0].text
    assert "raw-sql-marker" not in result.content[0].text
    acquire.assert_not_called()
    fake_client.query.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", [run_query, run_query_async])
async def test_python_query_rejects_raw_binary_binding(runner):
    with (
        patch("mcp_clickhouse.mcp_server._queries.clients._acquire_clickhouse_client") as acquire,
        pytest.raises(ToolError, match="name:Type"),
    ):
        result = runner("SELECT {value:UInt32}, $raw$", {"value": 13, "$raw$": b"raw SQL"})
        if asyncio.iscoroutine(result):
            await result

    acquire.assert_not_called()


@pytest.mark.asyncio
async def test_typed_parameters_preserve_percent_text():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "run_query",
            {
                "query": (
                    "SELECT {value:UInt32} AS value, '%(pattern)s' AS pattern, "
                    "'example' LIKE '%(pattern)s' AS matches"
                ),
                "params": {"value": 13},
            },
        )

    assert json.loads(result.content[0].text) == {
        "columns": ["value", "pattern", "matches"], "rows": [[13, "%(pattern)s", 0]]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", [run_query, run_query_async])
@pytest.mark.parametrize("params", [[13], {13: "value"}])
async def test_python_query_rejects_invalid_params_before_client_acquisition(runner, params):
    with (
        patch("mcp_clickhouse.clients._resolve_client_config", return_value={}),
        patch("mcp_clickhouse.mcp_server._queries.clients._acquire_clickhouse_client") as acquire,
        pytest.raises(ToolError, match="params must be an object with string keys"),
    ):
        result = runner("SELECT 1", params)
        if asyncio.iscoroutine(result):
            await result

    acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "SELECT " + "{a:" * 80000,
        "SELECT {id:UInt32} -- " + "{a:x " * 40000,
    ],
)
async def test_placeholder_flood_rejected_before_raw_regex_and_client_acquisition(query):
    with (
        patch("mcp_clickhouse.queries.external_bind_re", wraps=external_bind_re) as bind_regex,
        patch("mcp_clickhouse.mcp_server._queries.clients._acquire_clickhouse_client") as acquire,
    ):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "run_query",
                {"query": query, "params": {"value": 13, "other": "bound-value-marker"}},
                raise_on_error=False,
            )

    assert result.is_error
    assert "Too many unterminated" in result.content[0].text
    assert "{name:Type}" in result.content[0].text
    assert "bound-value-marker" not in result.content[0].text
    assert query not in result.content[0].text
    assert bind_regex.fullmatch.call_count > 0
    bind_regex.search.assert_not_called()
    acquire.assert_not_called()


@pytest.mark.parametrize(
    "name",
    ["id", "_id", "9id", "13", "id\u00e9", "$id", "id$", "$9id", "9id$", "$", "$$", "id\u00e9$"],
)
def test_incomplete_placeholder_detection_uses_driver_name_grammar(name):
    prefix = "{" + name + ":"
    expected = external_bind_re.fullmatch(prefix + "String}") is not None

    assert _has_unclosed_placeholder("SELECT {value:UInt32}, " + prefix * 80000) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", [run_query, run_query_async])
async def test_python_query_rejects_placeholder_flood(runner):
    with (
        patch("mcp_clickhouse.clients._resolve_client_config", return_value={}),
        patch("mcp_clickhouse.queries.external_bind_re", wraps=external_bind_re) as bind_regex,
        patch("mcp_clickhouse.mcp_server._queries.clients._acquire_clickhouse_client") as acquire,
        pytest.raises(ToolError, match="Too many unterminated"),
    ):
        result = runner("SELECT {value:UInt32}, " + "{other:" * 80000, {"value": 13})
        if asyncio.iscoroutine(result):
            await result

    assert bind_regex.fullmatch.call_count > 0
    bind_regex.search.assert_not_called()
    acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", [run_query, run_query_async])
@pytest.mark.parametrize("placeholder", ["{ id:UInt32}", "{id :UInt32}", "{ id : UInt32 }"])
async def test_spaced_placeholder_rejected_with_spaces_message(runner, placeholder):
    with (
        patch("mcp_clickhouse.clients._resolve_client_config", return_value={}),
        patch("mcp_clickhouse.mcp_server._queries.clients._acquire_clickhouse_client") as acquire,
        pytest.raises(ToolError, match="opening brace, name, and colon adjacent"),
    ):
        result = runner(f"SELECT {placeholder}", {"id": 13})
        if asyncio.iscoroutine(result):
            await result

    acquire.assert_not_called()


def test_sync_query_binds_parameters():
    assert json.loads(run_query("SELECT {value:UInt32} AS value", {"value": 79})) == {
        "columns": ["value"], "rows": [[79]]
    }


@pytest.mark.asyncio
async def test_parameter_values_stay_out_of_normal_query_logs(caplog):
    value = "parameter-value-log-marker"
    query = "SELECT {value:String} AS value"
    caplog.set_level(logging.INFO, logger="mcp_clickhouse.mcp_server")

    async with Client(mcp) as client:
        result = await client.call_tool("run_query", {"query": query, "params": {"value": value}})

    assert json.loads(result.content[0].text)["rows"] == [[value]]
    assert query in caplog.text
    assert value not in caplog.text


@pytest.mark.asyncio
async def test_parameter_names_do_not_trigger_drop_guard(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_ALLOW_WRITE_ACCESS", "true")
    monkeypatch.setenv("CLICKHOUSE_ALLOW_DROP", "false")
    async with Client(mcp) as client:
        result = await client.call_tool(
            "run_query",
            {
                "query": "SELECT {update:UInt32} AS n, {drop:String} AS s",
                "params": {"update": 13, "drop": "DELETE FROM example"},
            },
        )

    assert json.loads(result.content[0].text) == {
        "columns": ["n", "s"], "rows": [[13, "DELETE FROM example"]]
    }


@pytest.fixture
def parameter_table():
    table = f"mcp_parameters_{uuid.uuid4().hex}"
    client = create_clickhouse_client()
    try:
        client.command(f"CREATE TABLE {table} (value UInt32) ENGINE = Memory")
        yield client, table
    finally:
        try:
            client.command(f"DROP TABLE IF EXISTS {table}")
        finally:
            client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "allow_write,allow_drop", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_parameterized_writes_preserve_access_gates(
    monkeypatch, parameter_table, allow_write, allow_drop
):
    direct_client, table = parameter_table
    monkeypatch.setenv("CLICKHOUSE_ALLOW_WRITE_ACCESS", str(allow_write).lower())
    monkeypatch.setenv("CLICKHOUSE_ALLOW_DROP", str(allow_drop).lower())
    async with Client(mcp) as client:
        insert = await client.call_tool(
            "run_query",
            {"query": f"INSERT INTO {table} SELECT {{value:UInt32}}", "params": {"value": 79}},
            raise_on_error=False,
        )
        assert insert.is_error is not allow_write
        assert direct_client.command(f"SELECT count() FROM {table}") == int(allow_write)

        drop = await client.call_tool(
            "run_query",
            {"query": "DROP TABLE {table:Identifier}", "params": {"table": table}},
            raise_on_error=False,
        )
        assert drop.is_error is not (allow_write and allow_drop)
        if allow_write and not allow_drop:
            assert "CLICKHOUSE_ALLOW_DROP=true" in drop.content[0].text
        assert bool(direct_client.command(f"EXISTS TABLE {table}")) is not (allow_write and allow_drop)
