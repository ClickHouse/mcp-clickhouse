"""Unit tests for JSON serialization of tool results.

These are pure-function tests and do not require a ClickHouse connection.
"""

import json

from mcp_clickhouse.mcp_server import (
    JS_MAX_SAFE_INTEGER,
    _json_safe,
    _serialize_tool_result,
)


def test_large_uint64_is_stringified():
    # Value from the bug report: it would otherwise round to ...081000.
    value = 1875924584784080993
    result = json.loads(_serialize_tool_result({"columns": ["id"], "rows": [[value]]}))
    assert result["rows"][0][0] == str(value)


def test_large_negative_int_is_stringified():
    value = -(1 << 63)
    assert _json_safe(value) == str(value)


def test_boundary_values_stay_numeric():
    # The largest safe integer and everything below it must remain a JSON number.
    assert _json_safe(JS_MAX_SAFE_INTEGER) == JS_MAX_SAFE_INTEGER
    assert _json_safe(-JS_MAX_SAFE_INTEGER) == -JS_MAX_SAFE_INTEGER
    assert _json_safe(0) == 0
    assert _json_safe(42) == 42
    # One past the boundary flips to a string.
    assert _json_safe(JS_MAX_SAFE_INTEGER + 1) == str(JS_MAX_SAFE_INTEGER + 1)


def test_booleans_stay_boolean():
    # bool is an int subclass; it must never be stringified.
    assert _json_safe(True) is True
    assert _json_safe(False) is False


def test_nested_structures_are_walked():
    payload = {
        "rows": [
            (1875924584784080993, "web-01", [9999999999999999999, 7]),
        ],
        "meta": {"count": 2, "huge": 12345678901234567890},
    }
    result = json.loads(_serialize_tool_result(payload))
    assert result == {
        "rows": [["1875924584784080993", "web-01", ["9999999999999999999", 7]]],
        "meta": {"count": 2, "huge": "12345678901234567890"},
    }


def test_non_int_types_still_serialize_via_default():
    # Types json can't encode natively fall through to json.dumps(default=str).
    from decimal import Decimal

    result = json.loads(_serialize_tool_result({"price": Decimal("1.5")}))
    assert result["price"] == "1.5"
