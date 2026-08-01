"""Unit tests for JSON-safe serialization of tool results.

Regression coverage for https://github.com/ClickHouse/mcp-clickhouse/issues/111:
large ClickHouse integers (UInt64, Int64, UInt128, Int128, UInt256, Int256) were
serialized as raw JSON number literals. JSON itself has no size limit on numbers,
but most MCP clients decode results the way JavaScript's `JSON.parse` does, which
represents every JSON number as an IEEE 754 double. Integers larger than
2**53 - 1 (`Number.MAX_SAFE_INTEGER`) silently lose precision when decoded that
way -- e.g. the reporter's 1875924584784080993 becomes 1875924584784081000.

Python's own `json` module has arbitrary-precision integers, so a plain
`json.dumps` -> `json.loads` round trip in this test suite can't reproduce that
corruption directly. Instead, these tests assert on the *mechanism* of the fix:
out-of-range integers must be emitted as JSON strings (exact decimal text),
never as bare number literals, so that any float64-based JSON consumer receives
a string it can parse losslessly instead of a number it will round.

These are pure unit tests -- no ClickHouse connection required.
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mcp_clickhouse.mcp_server import _json_safe, _serialize_tool_result, execute_query

# The reporter's example from issue #111: a UInt64 value that exceeds
# Number.MAX_SAFE_INTEGER and gets rounded to 1875924584784081000 by JS.
BIG_UINT64 = 1875924584784080993
JS_MAX_SAFE_INTEGER = 2**53 - 1


class TestJsonSafe(unittest.TestCase):
    """Unit tests for the `_json_safe` helper in isolation."""

    def test_small_int_untouched(self):
        self.assertEqual(_json_safe(42), 42)
        self.assertIsInstance(_json_safe(42), int)

    def test_int_at_safe_boundary_untouched(self):
        result = _json_safe(JS_MAX_SAFE_INTEGER)
        self.assertEqual(result, JS_MAX_SAFE_INTEGER)
        self.assertIsInstance(result, int)

    def test_int_beyond_safe_boundary_stringified(self):
        result = _json_safe(JS_MAX_SAFE_INTEGER + 1)
        self.assertIsInstance(result, str)
        self.assertEqual(result, str(JS_MAX_SAFE_INTEGER + 1))

    def test_large_uint64_stringified(self):
        result = _json_safe(BIG_UINT64)
        self.assertIsInstance(result, str)
        self.assertEqual(result, str(BIG_UINT64))
        self.assertEqual(int(result), BIG_UINT64)

    def test_large_negative_int_stringified(self):
        big_negative = -BIG_UINT64
        result = _json_safe(big_negative)
        self.assertIsInstance(result, str)
        self.assertEqual(result, str(big_negative))

    def test_bool_untouched(self):
        # bool is a subclass of int; it must never become the string "True"/"False".
        self.assertIs(_json_safe(True), True)
        self.assertIs(_json_safe(False), False)

    def test_nested_dict_list_and_tuple(self):
        # Covers ClickHouse Array/Tuple/Map-shaped values nested in a result.
        nested = {
            "a": [1, BIG_UINT64, {"b": BIG_UINT64}],
            "c": (BIG_UINT64, "text"),
        }
        result = _json_safe(nested)
        self.assertEqual(result["a"][0], 1)
        self.assertEqual(result["a"][1], str(BIG_UINT64))
        self.assertEqual(result["a"][2]["b"], str(BIG_UINT64))
        self.assertEqual(result["c"], [str(BIG_UINT64), "text"])

    def test_non_numeric_types_untouched(self):
        self.assertEqual(_json_safe("hello"), "hello")
        self.assertIsNone(_json_safe(None))
        self.assertEqual(_json_safe(3.14), 3.14)

    def test_dict_keys_left_alone(self):
        # JSON object keys are always emitted as exact-text strings regardless
        # of magnitude, so only values need the safety conversion.
        result = _json_safe({BIG_UINT64: "value"})
        self.assertIn(BIG_UINT64, result)
        self.assertEqual(result[BIG_UINT64], "value")


class TestSerializeToolResult(unittest.TestCase):
    """Unit tests for `_serialize_tool_result`, the function every MCP tool funnels through."""

    def test_large_uint64_survives_json_round_trip_as_string(self):
        payload = {"columns": ["id"], "rows": [[BIG_UINT64]]}
        serialized = _serialize_tool_result(payload)
        decoded = json.loads(serialized)

        value = decoded["rows"][0][0]
        # Must be carried as a string (exact decimal text), not a bare JSON
        # number -- that's what protects it from a JS float64 `Number`.
        self.assertIsInstance(value, str)
        self.assertEqual(int(value), BIG_UINT64)

    def test_small_ints_stay_numeric(self):
        payload = {"columns": ["id"], "rows": [[1], [2]]}
        serialized = _serialize_tool_result(payload)
        decoded = json.loads(serialized)
        self.assertEqual(decoded["rows"], [[1], [2]])
        self.assertIsInstance(decoded["rows"][0][0], int)

    def test_non_serializable_type_still_falls_back_to_str(self):
        # Existing behavior (e.g. Decimal/UUID/datetime) must be preserved:
        # `default=str` still handles types json.dumps can't encode natively.
        class Weird:
            def __str__(self):
                return "weird-value"

        serialized = _serialize_tool_result({"value": Weird()})
        self.assertEqual(json.loads(serialized), {"value": "weird-value"})


class TestExecuteQueryBigIntRegression(unittest.TestCase):
    """End-to-end regression test through `execute_query` with a mocked ClickHouse client.

    Reproduces the reporter's scenario -- a `run_query` call whose result set
    contains a UInt64 value beyond the JS-safe integer range -- without needing
    a live ClickHouse server.
    """

    def test_execute_query_stringifies_large_uint64(self):
        mock_result = MagicMock()
        mock_result.column_names = ["big_value"]
        mock_result.result_rows = [(BIG_UINT64,)]

        mock_client = MagicMock()
        mock_client.server_settings = {}
        mock_client.query.return_value = mock_result

        # Bypass the real env-var-backed config singleton entirely so this
        # test is hermetic regardless of process env / test execution order.
        fake_config = SimpleNamespace(allow_write_access=False, allow_drop=False)

        with (
            patch("mcp_clickhouse.mcp_server.create_clickhouse_client", return_value=mock_client),
            patch("mcp_clickhouse.mcp_server.get_config", return_value=fake_config),
        ):
            result_json = execute_query("SELECT big_value FROM some_table")

        decoded = json.loads(result_json)
        self.assertEqual(decoded["columns"], ["big_value"])
        self.assertEqual(decoded["rows"], [[str(BIG_UINT64)]])
        self.assertEqual(int(decoded["rows"][0][0]), BIG_UINT64)


if __name__ == "__main__":
    unittest.main()
