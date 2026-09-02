"""Event-loop responsiveness coverage for the list_databases and list_tables MCP tools.

Mirrors test_run_query_does_not_block_other_mcp_requests in test_mcp_server.py, but gates
on threading.Event objects instead of wall-clock sleeps. The property under test is that
_run_metadata_tool hands the blocking sync helper to QUERY_EXECUTOR rather than running it
on the event loop thread, so a second MCP request can still be served while the helper is
parked. A wall-clock sleep only makes that likely; an event gate makes it deterministic.
"""

import asyncio
import json
import threading
from unittest.mock import patch

import pytest
from fastmcp import Client

from mcp_clickhouse import mcp_server


def _blocking_helper(
    entered: threading.Event,
    release: threading.Event,
    released_by_timeout: threading.Event,
    payload: str,
):
    """Build a fake sync helper: signal entry, block on release, then return payload.

    The wait is bounded. If the wrapper ran this helper on the event loop, nothing
    on the loop could ever set `release`, so an unbounded wait would hang the
    test instead of failing it; the bound turns that into a recorded failure.
    """

    def _helper(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=5):
            released_by_timeout.set()
        return payload

    return _helper


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "tool_args", "patch_target", "fake_payload"),
    [
        (
            "list_databases",
            {},
            "mcp_clickhouse.mcp_server._list_databases_with_config",
            json.dumps(["default", "system"]),
        ),
        (
            "list_tables",
            {"database": "default"},
            "mcp_clickhouse.mcp_server._list_tables_with_config",
            json.dumps({"tables": [], "next_page_token": None, "total_tables": 0}),
        ),
    ],
)
async def test_metadata_tool_does_not_block_other_mcp_requests(
    tool_name, tool_args, patch_target, fake_payload
):
    """The event loop keeps serving other MCP requests while the sync helper is parked.

    The patched helper never touches ClickHouse: it flips `entered`, blocks on `release`,
    then returns a fixed JSON payload. If the async wrapper ran the helper on the event
    loop thread instead of submitting it to QUERY_EXECUTOR, the ping below could not
    complete until `release` were set, because nothing would be free to service it. The
    5 second timeouts are only a hang guard; they are not a timing assertion.
    """
    entered = threading.Event()
    release = threading.Event()
    released_by_timeout = threading.Event()
    helper = _blocking_helper(entered, release, released_by_timeout, fake_payload)

    async with Client(mcp_server.mcp) as client:
        with patch(patch_target, side_effect=helper):
            blocked_task = asyncio.create_task(client.call_tool(tool_name, tool_args))
            try:
                entered_in_time = await asyncio.get_running_loop().run_in_executor(
                    None, entered.wait, 5
                )
                assert entered_in_time, f"{tool_name} helper was never entered"

                # While the worker thread is still parked on `release`, the event loop
                # must remain free to answer another request on the same client.
                # (client.ping() is not implemented by this FastMCP server, so
                # list_tools is used as the concurrent probe instead.)
                other_tools = await asyncio.wait_for(client.list_tools(), timeout=5)
                assert len(other_tools) >= 1
            finally:
                release.set()

            result = await asyncio.wait_for(blocked_task, timeout=5)

    assert not released_by_timeout.is_set(), (
        f"{tool_name} helper was released by its timeout: the event loop was blocked"
    )
    assert len(result.content) == 1
    assert result.content[0].text == fake_payload


@pytest.mark.asyncio
async def test_run_metadata_tool_runs_on_query_executor_not_event_loop_thread():
    """_run_metadata_tool must hand the sync helper to QUERY_EXECUTOR, not run it inline."""
    event_loop_thread_ident = threading.get_ident()
    worker_thread_ident = None

    def fake_helper() -> str:
        nonlocal worker_thread_ident
        worker_thread_ident = threading.current_thread().ident
        return "null"

    result = await mcp_server._run_metadata_tool("list_databases", fake_helper)

    assert result == "null"
    assert worker_thread_ident is not None
    assert worker_thread_ident != event_loop_thread_ident
