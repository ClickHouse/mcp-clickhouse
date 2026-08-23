"""Tests for the result-size bound on run_query.

`CLICKHOUSE_MCP_MAX_RESULT_ROWS` bounds how many rows a single query result may
contain. The query timeout bounds how long a query runs, but a fast query over a
large table finishes well inside the timeout and then floods the client, so size
needs its own bound.
"""

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from dotenv import load_dotenv

from mcp_clickhouse import create_clickhouse_client, run_query
from mcp_clickhouse.mcp_server import _stream_bounded_result

load_dotenv()

TOTAL_ROWS = 250


class _FakeStream:
    """Minimal stand-in for clickhouse-connect's row block stream."""

    def __init__(self, blocks, column_names=("a", "b")):
        self._blocks = blocks
        self.source = SimpleNamespace(column_names=column_names)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    def __iter__(self):
        return iter(self._blocks)


class _FakeClient:
    def __init__(self, blocks, column_names=("a", "b")):
        self.stream = _FakeStream(blocks, column_names)
        self.queries = []

    def query_row_block_stream(self, query, settings=None):
        self.queries.append((query, settings))
        return self.stream


def _rows(start, count):
    return [(i, f"v{i}") for i in range(start, start + count)]


@pytest.mark.parametrize(
    "blocks,max_rows,expected_rows,expected_truncated",
    [
        # Fewer rows than the bound.
        ([_rows(0, 3)], 10, 3, False),
        # Exactly the bound: complete, not truncated. This is the case a naive
        # "returned == max_rows" check gets wrong.
        ([_rows(0, 10)], 10, 10, False),
        # One row past the bound.
        ([_rows(0, 11)], 10, 10, True),
        # Bound falls inside a later block.
        ([_rows(0, 4), _rows(4, 4), _rows(8, 4)], 10, 10, True),
        # Bound falls exactly on a block boundary with more blocks behind it.
        ([_rows(0, 5), _rows(5, 5), _rows(10, 5)], 10, 10, True),
        # Empty result.
        ([], 10, 0, False),
        # Bound of one.
        ([_rows(0, 100)], 1, 1, True),
    ],
)
def test_stream_bounded_result(blocks, max_rows, expected_rows, expected_truncated):
    client = _FakeClient(blocks)

    columns, rows, truncated = _stream_bounded_result(client, "SELECT 1", {}, max_rows)

    assert len(rows) == expected_rows
    assert truncated is expected_truncated
    assert columns == ["a", "b"]


def test_stream_closes_even_when_stopping_early():
    client = _FakeClient([_rows(0, 100)])

    _stream_bounded_result(client, "SELECT 1", {}, 5)

    assert client.stream.closed


def test_stream_passes_query_settings_through():
    client = _FakeClient([_rows(0, 1)])

    _stream_bounded_result(client, "SELECT 1", {"readonly": "1"}, 10)

    assert client.queries == [("SELECT 1", {"readonly": "1"})]


class TestResultRowLimit(unittest.TestCase):
    """Integration coverage against a real ClickHouse server."""

    @classmethod
    def setUpClass(cls):
        cls.client = create_clickhouse_client()
        cls.test_db = "test_result_limit_db"
        cls.test_table = "rows"
        cls.total_rows = TOTAL_ROWS

        cls.client.command(f"DROP DATABASE IF EXISTS {cls.test_db}")
        cls.client.command(f"CREATE DATABASE {cls.test_db}")
        cls.client.command(f"""
            CREATE TABLE {cls.test_db}.{cls.test_table} (
                id UInt32,
                value String
            ) ENGINE = MergeTree()
            ORDER BY id
        """)
        cls.client.command(f"""
            INSERT INTO {cls.test_db}.{cls.test_table}
            SELECT number, concat('v', toString(number)) FROM numbers({cls.total_rows})
        """)

    @classmethod
    def tearDownClass(cls):
        cls.client.command(f"DROP DATABASE IF EXISTS {cls.test_db}")

    def _select_all(self):
        return f"SELECT * FROM {self.test_db}.{self.test_table} ORDER BY id"

    @patch.dict(os.environ, {"CLICKHOUSE_MCP_MAX_RESULT_ROWS": "50"})
    def test_large_result_is_truncated_and_flagged(self):
        result = json.loads(run_query(self._select_all()))

        self.assertEqual(len(result["rows"]), 50)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["max_result_rows"], 50)
        self.assertEqual(result["columns"], ["id", "value"])

    @patch.dict(os.environ, {"CLICKHOUSE_MCP_MAX_RESULT_ROWS": "50"})
    def test_truncation_returns_the_first_rows(self):
        result = json.loads(run_query(self._select_all()))

        self.assertEqual([row[0] for row in result["rows"]], list(range(50)))

    @patch.dict(os.environ, {"CLICKHOUSE_MCP_MAX_RESULT_ROWS": "1000"})
    def test_small_result_is_not_flagged(self):
        result = json.loads(run_query(self._select_all()))

        self.assertEqual(len(result["rows"]), self.total_rows)
        self.assertNotIn("truncated", result)
        self.assertNotIn("max_result_rows", result)

    @patch.dict(os.environ, {"CLICKHOUSE_MCP_MAX_RESULT_ROWS": str(TOTAL_ROWS)})
    def test_result_exactly_at_the_bound_is_not_truncated(self):
        """A table holding exactly max_result_rows rows is complete, not truncated."""
        result = json.loads(run_query(self._select_all()))

        self.assertEqual(len(result["rows"]), self.total_rows)
        self.assertNotIn("truncated", result)

    @patch.dict(os.environ, {"CLICKHOUSE_MCP_MAX_RESULT_ROWS": "0"})
    def test_zero_disables_the_bound(self):
        result = json.loads(run_query(self._select_all()))

        self.assertEqual(len(result["rows"]), self.total_rows)
        self.assertNotIn("truncated", result)

    @patch.dict(os.environ, {"CLICKHOUSE_MCP_MAX_RESULT_ROWS": "10"})
    def test_empty_result_is_not_flagged(self):
        query = f"SELECT * FROM {self.test_db}.{self.test_table} WHERE id = 999999"

        result = json.loads(run_query(query))

        self.assertEqual(result["rows"], [])
        self.assertNotIn("truncated", result)

    @patch.dict(os.environ, {"CLICKHOUSE_MCP_MAX_RESULT_ROWS": "10"})
    def test_column_names_survive_aliasing(self):
        query = f"SELECT id AS renamed, value FROM {self.test_db}.{self.test_table} LIMIT 3"

        result = json.loads(run_query(query))

        self.assertEqual(result["columns"], ["renamed", "value"])


if __name__ == "__main__":
    unittest.main()
