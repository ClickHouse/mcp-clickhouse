"""Contract tests for JSON-encoded tool results."""

import asyncio
import json
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastmcp import Client

from mcp_clickhouse.mcp_server import (
    _serialize_tool_result_with_simplejson,
    _serialize_tool_result_with_stdlib,
    _stringify_unsafe_integers,
    mcp,
    run_chdb_select_query,
    run_chdb_select_query_async,
)


@pytest.fixture(
    params=[_serialize_tool_result_with_simplejson, _serialize_tool_result_with_stdlib],
    ids=["simplejson", "stdlib-fallback"],
)
def serialize(request):
    return request.param


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-9007199254740991, -9007199254740991),
        (9007199254740991, 9007199254740991),
        (-9007199254740992, "-9007199254740992"),
        (9007199254740992, "9007199254740992"),
        (1875924584784080993, "1875924584784080993"),
        (-(1 << 63), str(-(1 << 63))),
        ((1 << 63) - 1, str((1 << 63) - 1)),
        ((1 << 64) - 1, str((1 << 64) - 1)),
        (-(1 << 127), str(-(1 << 127))),
        ((1 << 127) - 1, str((1 << 127) - 1)),
        ((1 << 128) - 1, str((1 << 128) - 1)),
    ],
)
def test_integer_contract(serialize, value, expected):
    result = json.loads(serialize({"value": value}))
    assert result["value"] == expected
    assert type(result["value"]) is type(expected)


def test_nested_rows_and_metadata_follow_integer_contract(serialize):
    result = json.loads(
        serialize(
            {
                "rows": [
                    (
                        1875924584784080993,
                        True,
                        [9007199254740992, 7],
                    )
                ],
                "metadata": {
                    "total_rows": (1 << 64) - 1,
                    "total_bytes": 42,
                    "enabled": False,
                },
            }
        )
    )

    assert result == {
        "rows": [["1875924584784080993", True, ["9007199254740992", 7]]],
        "metadata": {
            "total_rows": "18446744073709551615",
            "total_bytes": 42,
            "enabled": False,
        },
    }


def test_stdlib_fallback_reuses_safe_containers():
    nested_tuple = (1, True)
    nested_list = [2, nested_tuple]
    payload = {"rows": nested_list}

    result = _stringify_unsafe_integers(payload)

    assert result is payload
    assert result["rows"] is nested_list
    assert result["rows"][1] is nested_tuple


def test_stdlib_fallback_copies_only_unsafe_branches_without_mutation():
    unsafe_value = 1 << 63
    unsafe_key = 1 << 64
    safe_branch = {"value": 7}
    unsafe_tuple = (unsafe_value, 8)
    unsafe_branch = [safe_branch, unsafe_tuple]
    payload = {
        "safe": safe_branch,
        "unsafe": unsafe_branch,
        unsafe_key: "key stays numeric",
    }

    result = _stringify_unsafe_integers(payload)

    assert result is not payload
    assert result["safe"] is safe_branch
    assert result["unsafe"] is not unsafe_branch
    assert result["unsafe"][0] is safe_branch
    assert result["unsafe"][1] == [str(unsafe_value), 8]
    assert unsafe_key in result
    assert payload["unsafe"][1] is unsafe_tuple
    assert payload["unsafe"][1][0] == unsafe_value


def test_stdlib_fallback_handles_repeated_non_cyclic_aliases():
    shared = [1 << 63]
    payload = [shared, shared]

    result = _stringify_unsafe_integers(payload)

    assert result == [[str(1 << 63)], [str(1 << 63)]]


@pytest.mark.parametrize("container_type", ["list", "dict"])
def test_circular_references_keep_json_dumps_error(serialize, container_type):
    if container_type == "list":
        payload = [1 << 63]
        payload.append(payload)
    else:
        payload = {"value": 1 << 63}
        payload["self"] = payload

    with pytest.raises(ValueError, match="Circular reference detected"):
        serialize(payload)


def test_existing_json_dumps_compatibility(serialize):
    point = namedtuple("Point", ["x", "y"])(1, 2)

    class CustomValue:
        def __str__(self):
            return "custom"

    payload = {
        "decimal": Decimal("1.25"),
        "not_a_number": float("nan"),
        "positive_infinity": float("inf"),
        "negative_infinity": float("-inf"),
        "bytes": b"abc",
        "tuple": (1, 2),
        "namedtuple": point,
        "date": date(2026, 8, 31),
        "custom": CustomValue(),
        "boolean": True,
    }

    assert serialize(payload) == json.dumps(payload, default=str)


def test_unsupported_dict_keys_still_fail(serialize):
    with pytest.raises(TypeError):
        serialize({("unsupported",): "value"})


@pytest.mark.asyncio
async def test_registered_run_query_returns_exact_integer_boundary_text():
    class FakeClient:
        server_settings = {}

        def query(self, _query, settings):
            assert settings["query_id"]
            return SimpleNamespace(
                column_names=["safe_min", "unsafe_min", "safe_max", "unsafe_max"],
                result_rows=[
                    (
                        -9007199254740991,
                        -9007199254740992,
                        9007199254740991,
                        9007199254740992,
                    )
                ],
            )

    entry = SimpleNamespace(client=FakeClient())
    expected = (
        '{"columns": ["safe_min", "unsafe_min", "safe_max", "unsafe_max"], '
        '"rows": [[-9007199254740991, "-9007199254740992", 9007199254740991, '
        '"9007199254740992"]]}'
    )

    with (
        patch("mcp_clickhouse.mcp_server._acquire_clickhouse_client", return_value=entry),
        patch("mcp_clickhouse.mcp_server._release_client_entry"),
    ):
        async with Client(mcp) as client:
            result = await client.call_tool("run_query", {"query": "SELECT boundaries"})

    assert result.content[0].text == expected


def test_chdb_tool_serializes_nested_wide_integers():
    chdb_result = [
        {
            "wide": (1 << 128) - 1,
            "nested": [-(1 << 127), 9007199254740991, True],
        }
    ]

    with patch("mcp_clickhouse.mcp_server.execute_chdb_query", return_value=chdb_result):
        result = json.loads(run_chdb_select_query("SELECT values"))

    assert result == [
        {
            "wide": str((1 << 128) - 1),
            "nested": [str(-(1 << 127)), 9007199254740991, True],
        }
    ]


@pytest.mark.asyncio
async def test_async_chdb_result_processing_runs_off_event_loop():
    event_loop_thread_id = threading.get_ident()
    processing_thread_id = None

    def process_result(_result):
        nonlocal processing_thread_id
        processing_thread_id = threading.get_ident()
        return '[{"value": 1}]'

    with (
        patch("mcp_clickhouse.mcp_server.execute_chdb_query", return_value=[{"value": 1}]),
        patch("mcp_clickhouse.mcp_server._process_chdb_result", side_effect=process_result),
    ):
        result = await run_chdb_select_query_async("SELECT 1")

    assert result == '[{"value": 1}]'
    assert processing_thread_id != event_loop_thread_id


@pytest.mark.asyncio
async def test_async_chdb_processing_does_not_wait_for_query_worker():
    first_query_started = threading.Event()
    release_first_query = threading.Event()
    second_query_submitted = threading.Event()
    second_query_started = threading.Event()
    release_second_query = threading.Event()
    first_processing_started = threading.Event()

    class TrackingExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            if args and args[0] == "second":
                second_query_submitted.set()
            return future

    def execute_query(query):
        if query == "first":
            first_query_started.set()
            assert release_first_query.wait(timeout=1)
            return [{"value": 1}]
        second_query_started.set()
        assert release_second_query.wait(timeout=1)
        return [{"value": 2}]

    def process_result(result):
        if result == [{"value": 1}]:
            first_processing_started.set()
        return json.dumps(result)

    async def event_was_set(event):
        return await asyncio.to_thread(event.wait, 1)

    query_executor = TrackingExecutor(max_workers=1)
    first_task = None
    second_task = None
    try:
        with (
            patch("mcp_clickhouse.mcp_server.QUERY_EXECUTOR", query_executor),
            patch("mcp_clickhouse.mcp_server.execute_chdb_query", side_effect=execute_query),
            patch("mcp_clickhouse.mcp_server._process_chdb_result", side_effect=process_result),
            patch(
                "mcp_clickhouse.mcp_server.get_mcp_config",
                return_value=SimpleNamespace(query_timeout=2),
            ),
        ):
            first_task = asyncio.create_task(run_chdb_select_query_async("first"))
            assert await event_was_set(first_query_started)

            second_task = asyncio.create_task(run_chdb_select_query_async("second"))
            assert await event_was_set(second_query_submitted)
            release_first_query.set()

            assert await event_was_set(second_query_started)
            assert await event_was_set(first_processing_started)
            release_second_query.set()

            assert await first_task == '[{"value": 1}]'
            assert await second_task == '[{"value": 2}]'
    finally:
        release_first_query.set()
        release_second_query.set()
        tasks = [task for task in (first_task, second_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        query_executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_async_chdb_serialization_is_not_part_of_query_timeout():
    def slow_process_result(_result):
        time.sleep(0.2)
        return '[{"value": 1}]'

    with (
        patch("mcp_clickhouse.mcp_server.execute_chdb_query", return_value=[{"value": 1}]),
        patch("mcp_clickhouse.mcp_server._process_chdb_result", side_effect=slow_process_result),
        patch(
            "mcp_clickhouse.mcp_server.get_mcp_config",
            return_value=SimpleNamespace(query_timeout=0.05),
        ),
    ):
        result = await run_chdb_select_query_async("SELECT 1")

    assert result == '[{"value": 1}]'
