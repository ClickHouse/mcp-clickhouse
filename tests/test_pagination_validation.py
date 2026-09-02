"""Tests for list_tables input validation."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest
from cachetools import TTLCache
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_clickhouse.mcp_server import (
    _claim_page_token_for_request,
    _list_tables_with_config,
    _restore_page_token,
    _table_pagination_cache_lock,
    create_page_token,
    mcp,
    table_pagination_cache,
)


@pytest.mark.parametrize("page_size", [0, -1])
@pytest.mark.asyncio
async def test_list_tables_rejects_non_positive_page_size(page_size):
    """Reject page sizes that cannot produce a valid page through MCP."""
    with patch("mcp_clickhouse.mcp_server._acquire_clickhouse_client") as acquire_client:
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="greater than 0"):
                await client.call_tool(
                    "list_tables",
                    {"database": "database", "page_size": page_size},
                )

    acquire_client.assert_not_called()


@pytest.mark.asyncio
async def test_list_tables_page_size_schema_requires_positive_value():
    """Advertise the positive page size requirement to MCP clients."""
    async with Client(mcp) as client:
        tools = await client.list_tools()

    list_tables_tool = next(tool for tool in tools if tool.name == "list_tables")
    page_size_schema = list_tables_tool.input_schema["properties"]["page_size"]

    assert page_size_schema["exclusiveMinimum"] == 0
    assert "one hour" in list_tables_tool.description


def test_duplicate_page_token_has_only_one_concurrent_claim():
    barrier = threading.Barrier(2)
    start_indexes = []
    start_indexes_lock = threading.Lock()
    with _table_pagination_cache_lock:
        table_pagination_cache.clear()
    token = create_page_token(
        "database",
        None,
        None,
        ["first", "second"],
        1,
        True,
    )

    def paginated_data(_client, _database, _table_names, start_idx, _page_size, _details):
        with start_indexes_lock:
            start_indexes.append(start_idx)
        barrier.wait(timeout=1)
        return [], start_idx, False

    entries = [MagicMock(client=MagicMock()), MagicMock(client=MagicMock())]
    try:
        with (
            patch(
                "mcp_clickhouse.mcp_server._acquire_clickhouse_client",
                side_effect=entries,
            ),
            patch("mcp_clickhouse.mcp_server._release_client_entry"),
            patch(
                "mcp_clickhouse.mcp_server.fetch_table_names_from_system",
                return_value=["first", "second"],
            ),
            patch(
                "mcp_clickhouse.mcp_server.get_paginated_table_data",
                side_effect=paginated_data,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [
                executor.submit(
                    _list_tables_with_config,
                    {},
                    "database",
                    None,
                    None,
                    token,
                    1,
                    True,
                )
                for _ in range(2)
            ]
            results = [json.loads(future.result(timeout=2)) for future in futures]

        assert start_indexes.count(1) == 1
        assert start_indexes.count(0) == 1
        assert all(result["tables"] == [] for result in results)
    finally:
        with _table_pagination_cache_lock:
            table_pagination_cache.clear()


@pytest.mark.parametrize(
    ("database", "like", "not_like", "include_detailed_columns"),
    [
        ("other_database", None, None, True),
        ("database", "other%", None, True),
        ("database", None, "other%", True),
        ("database", None, None, False),
    ],
)
def test_page_token_mismatch_retains_original_token(
    database,
    like,
    not_like,
    include_detailed_columns,
):
    with _table_pagination_cache_lock:
        table_pagination_cache.clear()
    token = create_page_token(
        "database",
        None,
        None,
        ["first", "second"],
        1,
        True,
    )
    with _table_pagination_cache_lock:
        original_state = dict(table_pagination_cache[token])

    entry = MagicMock(client=MagicMock())
    try:
        with (
            patch(
                "mcp_clickhouse.mcp_server._acquire_clickhouse_client",
                return_value=entry,
            ),
            patch("mcp_clickhouse.mcp_server._release_client_entry"),
            patch(
                "mcp_clickhouse.mcp_server.fetch_table_names_from_system",
                return_value=[],
            ),
            patch(
                "mcp_clickhouse.mcp_server.get_paginated_table_data",
                return_value=([], 0, False),
            ),
            patch(
                "mcp_clickhouse.mcp_server._restore_page_token",
                wraps=_restore_page_token,
            ) as restore_page_token,
        ):
            result = json.loads(
                _list_tables_with_config(
                    {},
                    database,
                    like,
                    not_like,
                    token,
                    1,
                    include_detailed_columns,
                )
            )

        assert result == {
            "tables": [],
            "next_page_token": None,
            "total_tables": 0,
        }
        restore_page_token.assert_not_called()
        with _table_pagination_cache_lock:
            assert table_pagination_cache[token] == original_state
    finally:
        with _table_pagination_cache_lock:
            table_pagination_cache.clear()


def test_mismatch_failure_cannot_restore_token_after_valid_caller_consumes_it():
    mismatch_page_started = threading.Event()
    valid_call_finished = threading.Event()
    saved_indexes = []
    saved_indexes_lock = threading.Lock()
    with _table_pagination_cache_lock:
        table_pagination_cache.clear()
    token = create_page_token(
        "database",
        None,
        None,
        ["first", "second", "third"],
        2,
        True,
    )
    entries = [
        MagicMock(client=MagicMock()),
        MagicMock(client=MagicMock()),
        MagicMock(client=MagicMock()),
    ]

    def paginated_data(_client, database, _table_names, start_idx, _page_size, _details):
        if database == "wrong_database":
            assert start_idx == 0
            mismatch_page_started.set()
            assert valid_call_finished.wait(timeout=2)
            raise RuntimeError("mismatched request failed")
        with saved_indexes_lock:
            saved_indexes.append(start_idx)
        return [], start_idx, False

    try:
        with (
            patch(
                "mcp_clickhouse.mcp_server._acquire_clickhouse_client",
                side_effect=entries,
            ),
            patch("mcp_clickhouse.mcp_server._release_client_entry"),
            patch(
                "mcp_clickhouse.mcp_server.fetch_table_names_from_system",
                return_value=["first", "second", "third"],
            ),
            patch(
                "mcp_clickhouse.mcp_server.get_paginated_table_data",
                side_effect=paginated_data,
            ),
            patch(
                "mcp_clickhouse.mcp_server._restore_page_token",
                wraps=_restore_page_token,
            ) as restore_page_token,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            mismatch = executor.submit(
                _list_tables_with_config,
                {},
                "wrong_database",
                None,
                None,
                token,
                1,
                True,
            )
            assert mismatch_page_started.wait(timeout=2)
            try:
                valid_result = json.loads(
                    _list_tables_with_config(
                        {}, "database", None, None, token, 1, True
                    )
                )
            finally:
                valid_call_finished.set()

            with pytest.raises(RuntimeError, match="mismatched request failed"):
                mismatch.result(timeout=2)

            third_result = json.loads(
                _list_tables_with_config(
                    {}, "database", None, None, token, 1, True
                )
            )

        assert valid_result["total_tables"] == 3
        assert third_result["total_tables"] == 3
        assert saved_indexes == [2, 0]
        restore_page_token.assert_not_called()
        with _table_pagination_cache_lock:
            assert token not in table_pagination_cache
    finally:
        valid_call_finished.set()
        with _table_pagination_cache_lock:
            table_pagination_cache.clear()


def test_restored_page_token_keeps_original_expiration_deadline():
    current_time = [100.0]
    cache = TTLCache(maxsize=100, ttl=10, timer=lambda: current_time[0])

    with patch("mcp_clickhouse.mcp_server.table_pagination_cache", cache):
        token = create_page_token(
            "database",
            None,
            None,
            ["first", "second"],
            1,
            True,
        )
        current_time[0] = 109.9
        state = _claim_page_token_for_request(
            token,
            "database",
            None,
            None,
            True,
        )
        assert state is not None
        _restore_page_token(token, state)
        assert token in cache

        current_time[0] = 110.1
        assert token in cache
        assert (
            _claim_page_token_for_request(
                token,
                "database",
                None,
                None,
                True,
            )
            is None
        )
        assert token not in cache

        _restore_page_token(token, state)
        assert token not in cache


def test_page_token_cursor_survives_connection_retry():
    with _table_pagination_cache_lock:
        table_pagination_cache.clear()
    token = create_page_token(
        "database",
        None,
        None,
        ["first", "second", "third"],
        2,
        True,
    )
    clients = [MagicMock(), MagicMock()]
    entries = [MagicMock(client=client) for client in clients]
    start_indexes = []

    def paginated_data(_client, _database, _table_names, start_idx, _page_size, _details):
        start_indexes.append(start_idx)
        if len(start_indexes) == 1:
            raise ConnectionError("connection failed")
        return [], start_idx, False

    try:
        with (
            patch(
                "mcp_clickhouse.mcp_server._acquire_clickhouse_client",
                side_effect=entries,
            ),
            patch("mcp_clickhouse.mcp_server._release_client_entry"),
            patch("mcp_clickhouse.mcp_server._evict_cached_client"),
            patch(
                "mcp_clickhouse.mcp_server.fetch_table_names_from_system"
            ) as fetch_names,
            patch(
                "mcp_clickhouse.mcp_server.get_paginated_table_data",
                side_effect=paginated_data,
            ),
        ):
            result = json.loads(
                _list_tables_with_config(
                    {}, "database", None, None, token, 1, True
                )
            )

        assert start_indexes == [2, 2]
        assert result == {
            "tables": [],
            "next_page_token": None,
            "total_tables": 3,
        }
        fetch_names.assert_not_called()
    finally:
        with _table_pagination_cache_lock:
            table_pagination_cache.clear()


def test_page_token_is_restored_after_final_page_fetch_failure():
    with _table_pagination_cache_lock:
        table_pagination_cache.clear()
    token = create_page_token(
        "database",
        None,
        None,
        ["first", "second", "third"],
        2,
        True,
    )
    with _table_pagination_cache_lock:
        original_state = dict(table_pagination_cache[token])
    clients = [MagicMock(), MagicMock()]
    entries = [MagicMock(client=client) for client in clients]
    start_indexes = []

    def fail_page_fetch(_client, _database, _table_names, start_idx, _page_size, _details):
        start_indexes.append(start_idx)
        raise ConnectionError("connection failed")

    try:
        with (
            patch(
                "mcp_clickhouse.mcp_server._acquire_clickhouse_client",
                side_effect=entries,
            ),
            patch("mcp_clickhouse.mcp_server._release_client_entry"),
            patch("mcp_clickhouse.mcp_server._evict_cached_client"),
            patch(
                "mcp_clickhouse.mcp_server.fetch_table_names_from_system"
            ) as fetch_names,
            patch(
                "mcp_clickhouse.mcp_server.get_paginated_table_data",
                side_effect=fail_page_fetch,
            ),
        ):
            with pytest.raises(ConnectionError, match="connection failed"):
                _list_tables_with_config(
                    {}, "database", None, None, token, 1, True
                )

        assert start_indexes == [2, 2]
        fetch_names.assert_not_called()
        with _table_pagination_cache_lock:
            assert table_pagination_cache[token] == original_state
    finally:
        with _table_pagination_cache_lock:
            table_pagination_cache.clear()


@pytest.mark.parametrize("resume_from_token", [False, True])
@pytest.mark.asyncio
async def test_cancelled_mcp_call_does_not_commit_page_cursor(resume_from_token):
    page_started = threading.Event()
    release_page = threading.Event()
    page_finished = threading.Event()
    with _table_pagination_cache_lock:
        table_pagination_cache.clear()

    page_token = None
    original_state = None
    if resume_from_token:
        page_token = create_page_token(
            "database",
            None,
            None,
            ["first", "second", "third"],
            1,
            True,
        )
        with _table_pagination_cache_lock:
            original_state = dict(table_pagination_cache[page_token])

    def blocked_page(_client, _database, _table_names, start_idx, _page_size, _details):
        page_started.set()
        try:
            assert release_page.wait(timeout=2)
            return [], start_idx + 1, True
        finally:
            page_finished.set()

    entry = MagicMock(client=MagicMock())
    try:
        with (
            patch(
                "mcp_clickhouse.mcp_server._acquire_clickhouse_client",
                return_value=entry,
            ),
            patch("mcp_clickhouse.mcp_server._release_client_entry"),
            patch(
                "mcp_clickhouse.mcp_server.fetch_table_names_from_system",
                return_value=["first", "second", "third"],
            ),
            patch(
                "mcp_clickhouse.mcp_server.get_paginated_table_data",
                side_effect=blocked_page,
            ),
        ):
            async with Client(mcp) as client:
                call = asyncio.create_task(
                    client.call_tool(
                        "list_tables",
                        {
                            "database": "database",
                            "page_token": page_token,
                            "page_size": 1,
                        },
                    )
                )
                assert await asyncio.to_thread(page_started.wait, 2)
                call.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await call

            release_page.set()
            assert await asyncio.to_thread(page_finished.wait, 2)

        with _table_pagination_cache_lock:
            if resume_from_token:
                assert dict(table_pagination_cache) == {page_token: original_state}
            else:
                assert not table_pagination_cache
    finally:
        release_page.set()
        with _table_pagination_cache_lock:
            table_pagination_cache.clear()


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("list_databases", {"unexpected": True}),
        ("list_tables", {"database": "database", "page_size": 0}),
    ],
)
@pytest.mark.asyncio
async def test_registered_validation_errors_keep_public_tool_names(tool_name, arguments):
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(tool_name, arguments)

    assert f"call[{tool_name}]" in str(exc_info.value)
    assert "_async" not in str(exc_info.value)
