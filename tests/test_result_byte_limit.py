"""Byte-budgeted result encoding and the registered MCP query boundary."""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_clickhouse import mcp_server, serialization
from mcp_clickhouse.clients import _ClientCacheEntry
from mcp_clickhouse.mcp_env import MCPServerConfig
from mcp_clickhouse.serialization import _serialize_limited_query_result


@pytest.fixture(params=["stdlib", "simplejson"])
def encoder(request, monkeypatch):
    encode = (
        serialization._serialize_tool_result_with_stdlib
        if request.param == "stdlib"
        else serialization._serialize_tool_result_with_simplejson
    )
    monkeypatch.setattr(serialization, "_serialize_tool_result", encode)
    return encode


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [(1,), (2,)],
        [('quoted " text\n',), ("\u4e2d\u6587\U0001f525",)],
        [(2**63, Decimal("1.250"), date(2026, 11, 23), True, None)],
    ],
)
def test_complete_result_keeps_exact_encoding_at_budget_boundary(encoder, rows):
    columns = ["value"] if not rows else [f"value_{i}" for i in range(len(rows[0]))]
    expected = encoder({"columns": columns, "rows": rows})
    result = _serialize_limited_query_result(columns, iter(rows), len(expected.encode("utf-8")))
    assert result == expected
    assert "truncated" not in json.loads(result)


def test_overflow_stops_consumption_and_reserves_marker_space(encoder):
    consumed = []

    def rows():
        consumed.append(1)
        yield ["first"]
        consumed.append(2)
        yield ["x" * 1000]
        raise AssertionError("Rows after the overflow must not be consumed")

    result = _serialize_limited_query_result(["value"], rows(), 150)
    parsed = json.loads(result)
    assert parsed == {
        "columns": ["value"],
        "rows": [["first"]],
        "truncated": True,
        "max_result_bytes": 150,
    }
    assert len(result.encode("utf-8")) <= 150
    assert consumed == [1, 2]


def test_rows_are_removed_when_truncation_metadata_needs_space(encoder):
    rows = [["x" * 25], ["y" * 25], ["z" * 1000]]
    result = _serialize_limited_query_result(["value"], iter(rows), 110)
    parsed = json.loads(result)
    assert parsed["truncated"] is True
    assert parsed["rows"] == rows[: len(parsed["rows"])]
    assert len(parsed["rows"]) < 2
    assert len(result.encode("utf-8")) <= 110


def test_escaped_unicode_and_large_integer_rows_remain_complete(encoder):
    rows = [["\u4e2d\u6587\U0001f525", 2**63], ["\U0001f525" * 1000, 2**63]]
    result = _serialize_limited_query_result(["text", "id"], rows, 220)
    parsed = json.loads(result)
    assert parsed["rows"] == [["\u4e2d\u6587\U0001f525", str(2**63)]]
    assert parsed["truncated"] is True
    assert len(result.encode("utf-8")) <= 220


def test_a_single_oversized_row_returns_an_explicit_empty_prefix(encoder):
    result = _serialize_limited_query_result(["value"], [["x" * 1000]], 110)
    assert json.loads(result)["rows"] == []
    assert json.loads(result)["truncated"] is True
    assert len(result.encode("utf-8")) <= 110


def test_schema_larger_than_budget_fails_before_consuming_rows(encoder):
    def rows():
        raise AssertionError("The schema must be checked before reading rows")
        yield

    with pytest.raises(ValueError, match="columns exceed"):
        _serialize_limited_query_result(["column" * 100], rows(), 100)


def test_tiny_budget_reports_marker_overhead(encoder):
    empty = encoder({"columns": ["v"], "rows": []})
    with pytest.raises(ValueError, match="truncation marker"):
        _serialize_limited_query_result(["v"], [["x" * 1000]], len(empty.encode("utf-8")))


@pytest.mark.parametrize("value", ["-1", "1.5", "bad", ""])
def test_result_byte_configuration_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("CLICKHOUSE_MCP_MAX_RESULT_BYTES", value)
    with pytest.raises(ValueError, match="must be a non-negative integer"):
        _ = MCPServerConfig().max_result_bytes


def test_result_byte_configuration_rejects_unrepresentable_budget(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_MCP_MAX_RESULT_BYTES", "1")
    with pytest.raises(ValueError, match="at least 27 bytes"):
        _ = MCPServerConfig().max_result_bytes


def test_result_byte_configuration_defaults_to_unlimited(monkeypatch):
    monkeypatch.delenv("CLICKHOUSE_MCP_MAX_RESULT_BYTES", raising=False)
    assert MCPServerConfig().max_result_bytes == 0
    monkeypatch.setenv("CLICKHOUSE_MCP_MAX_RESULT_BYTES", "1024")
    assert MCPServerConfig().max_result_bytes == 1024


class FakeStream:
    def __init__(self, columns, blocks, error=None):
        self.source = SimpleNamespace(column_names=columns)
        self.blocks = blocks
        self.error = error
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def __iter__(self):
        yield from self.blocks
        if self.error:
            raise self.error


@pytest.fixture
def query_backend(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_MCP_MAX_RESULT_BYTES", "150")
    monkeypatch.setenv("CLICKHOUSE_ALLOW_WRITE_ACCESS", "false")
    client = MagicMock(server_version="26.3")
    client.server_settings = {}
    stream = FakeStream(["value"], [[["first"], ["x" * 1000]]])
    client.query_row_block_stream.return_value = stream
    client.query.return_value = SimpleNamespace(column_names=["value"], result_rows=[["x" * 1000]])
    entry = _ClientCacheEntry(client, 0, active_users=1)
    acquire = MagicMock(return_value=entry)
    monkeypatch.setattr(mcp_server._queries.clients, "_acquire_clickhouse_client", acquire)
    return client, stream, entry, acquire


def test_worker_preserves_sql_parameters_readonly_and_closes_stream(query_backend):
    client, stream, entry, _ = query_backend
    query = "SELECT {value:String} AS value"
    params = {"value": "first"}
    result = mcp_server._queries.execute_query(query, "byte-limit-worker", {}, params)
    assert len(result.encode("utf-8")) <= 150
    assert json.loads(result)["truncated"] is True
    args, kwargs = client.query_row_block_stream.call_args
    assert args == (query,)
    assert kwargs["parameters"] == params
    assert kwargs["settings"]["readonly"] == "1"
    assert kwargs["settings"]["query_id"] == "byte-limit-worker"
    client.query.assert_not_called()
    assert stream.closed
    assert entry.active_users == 0
    assert "byte-limit-worker" not in mcp_server._queries.active_queries


def test_zero_limit_retains_legacy_query_path(monkeypatch, query_backend):
    client, stream, entry, _ = query_backend
    monkeypatch.setenv("CLICKHOUSE_MCP_MAX_RESULT_BYTES", "0")
    result = json.loads(mcp_server._queries.execute_query("SELECT 1", "unlimited-worker", {}))
    assert result == {"columns": ["value"], "rows": [["x" * 1000]]}
    client.query.assert_called_once()
    client.query_row_block_stream.assert_not_called()
    assert not stream.closed
    assert entry.active_users == 0


def test_stream_errors_release_the_client_and_query_state(query_backend):
    client, _, entry, _ = query_backend
    stream = FakeStream(["value"], [[["first"]]], RuntimeError("stream failure"))
    client.query_row_block_stream.return_value = stream
    with pytest.raises(ToolError, match="stream failure"):
        mcp_server._queries.execute_query("SELECT 1", "broken-stream-worker", {})
    assert stream.closed
    assert entry.active_users == 0
    assert "broken-stream-worker" not in mcp_server._queries.active_queries


def test_encoding_errors_close_stream_and_release_client(monkeypatch, query_backend):
    _, stream, entry, _ = query_backend
    stream.source.column_names = ["wide_column" * 100]
    with pytest.raises(ToolError, match="columns exceed"):
        mcp_server._queries.execute_query("SELECT 1", "tiny-limit-worker", {})
    assert stream.closed
    assert entry.active_users == 0


def test_invalid_configuration_fails_before_client_acquisition(monkeypatch, query_backend):
    _, _, _, acquire = query_backend
    monkeypatch.setenv("CLICKHOUSE_MCP_MAX_RESULT_BYTES", "bad")
    with pytest.raises(ToolError, match="non-negative integer"):
        mcp_server._queries.execute_query("SELECT 1", "invalid-limit-worker", {})
    acquire.assert_not_called()


@pytest.mark.asyncio
async def test_registered_mcp_tool_returns_bounded_json_and_truncation_hint(query_backend):
    _, stream, entry, _ = query_backend
    async with Client(mcp_server.mcp) as client:
        tools = await client.list_tools()
        description = next(tool.description for tool in tools if tool.name == "run_query")
        assert "truncated=true" in description
        result = await client.call_tool("run_query", {"query": "SELECT 1"})
    text = result.content[0].text
    assert isinstance(text, str)
    assert len(text.encode("utf-8")) <= 150
    assert json.loads(text)["truncated"] is True
    assert stream.closed
    assert entry.active_users == 0
