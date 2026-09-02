"""Tests for query ID tracking and server-side cancellation."""

import asyncio
import concurrent.futures
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from mcp_clickhouse import mcp_server
from tests.helpers import fake_clickhouse_client
from mcp_clickhouse.mcp_server import (
    _ActiveQueryState,
    _ClientCacheEntry,
    _active_queries,
    _active_queries_lock,
    _cancel_query,
    _cancel_query_async,
    _resolve_client_config,
    _run_metadata_tool,
    execute_query,
    run_query,
    run_query_async,
)


class TestQueryIdTracking:
    """Tests for query_id propagation through execute_query."""

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_query_id_passed_in_settings(self, mock_cc):
        """query_id should be included in the settings dict passed to client.query()."""
        mock_client = fake_clickhouse_client("24.1")
        mock_client.server_settings = {}
        mock_result = MagicMock()
        mock_result.result_rows = [("row1",)]
        mock_result.column_names = ["col1"]
        mock_client.query.return_value = mock_result
        mock_cc.get_client.return_value = mock_client

        config = _resolve_client_config()
        execute_query("SELECT 1", "test-query-id-123", config)

        # Verify query_id was passed in settings
        call_args = mock_client.query.call_args
        settings = call_args[1].get("settings") or call_args.kwargs.get("settings")
        assert settings["query_id"] == "test-query-id-123"

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_active_queries_tracked_and_cleaned(self, mock_cc):
        """execute_query should register in _active_queries and clean up on completion."""
        mock_client = fake_clickhouse_client("24.1")
        mock_client.server_settings = {}
        mock_result = MagicMock()
        mock_result.result_rows = []
        mock_result.column_names = []
        mock_client.query.return_value = mock_result
        mock_cc.get_client.return_value = mock_client

        config = _resolve_client_config()

        # Before execution
        with _active_queries_lock:
            assert "tracking-test-id" not in _active_queries

        execute_query("SELECT 1", "tracking-test-id", config)

        # After completion, should be cleaned up
        with _active_queries_lock:
            assert "tracking-test-id" not in _active_queries

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_active_queries_cleaned_on_error(self, mock_cc):
        """execute_query should clean up _active_queries even on error."""
        mock_client = fake_clickhouse_client("24.1")
        mock_client.server_settings = {}
        mock_client.query.side_effect = Exception("DB error")
        mock_cc.get_client.return_value = mock_client

        config = _resolve_client_config()

        with pytest.raises(ToolError):
            execute_query("SELECT bad", "error-test-id", config)

        with _active_queries_lock:
            assert "error-test-id" not in _active_queries


class TestCancelQuery:
    """Tests for _cancel_query server-side cancellation."""

    def test_cancel_issues_kill_query(self):
        """_cancel_query should issue KILL QUERY via the cached client."""
        mock_client = MagicMock()
        client_entry = _ClientCacheEntry(mock_client, 0)
        query_id = str(uuid.uuid4())

        with _active_queries_lock:
            _active_queries[query_id] = _ActiveQueryState(
                "SELECT sleep(60)", client_entry=client_entry
            )

        _cancel_query(query_id)

        mock_client.command.assert_called_once_with(
            f"KILL QUERY WHERE query_id = '{query_id}'"
        )
        with _active_queries_lock:
            assert _active_queries[query_id].cancelled is True

    def test_cancel_noop_for_completed_query(self):
        """_cancel_query should be a no-op if the query already completed."""
        # No entry in _active_queries
        _cancel_query(str(uuid.uuid4()))  # Should not raise

    def test_cancel_warns_for_closed_client(self):
        """_cancel_query should log a warning if the query client is closed."""
        client_entry = _ClientCacheEntry(MagicMock(), 0, closed=True)
        query_id = str(uuid.uuid4())
        with _active_queries_lock:
            _active_queries[query_id] = _ActiveQueryState(
                "SELECT 1", client_entry=client_entry
            )

        _cancel_query(query_id)  # Should not raise

        with _active_queries_lock:
            assert _active_queries[query_id].cancelled is True

    def test_cancel_failure_does_not_raise(self):
        """_cancel_query should swallow exceptions from KILL QUERY."""
        mock_client = MagicMock()
        mock_client.command.side_effect = Exception("Permission denied")
        client_entry = _ClientCacheEntry(mock_client, 0)
        query_id = str(uuid.uuid4())

        with _active_queries_lock:
            _active_queries[query_id] = _ActiveQueryState(
                "SELECT 1", client_entry=client_entry
            )

        _cancel_query(query_id)  # Should not raise

    @patch("mcp_clickhouse.mcp_server.format_query_value", return_value="'bound-id'")
    def test_cancel_formats_uuid_as_query_value(self, mock_format_query_value):
        mock_client = MagicMock()
        client_entry = _ClientCacheEntry(mock_client, 0)
        query_id = str(uuid.uuid4())
        with _active_queries_lock:
            _active_queries[query_id] = _ActiveQueryState(
                "SELECT 1", client_entry=client_entry
            )

        _cancel_query(query_id)

        mock_format_query_value.assert_called_once_with(query_id)
        mock_client.command.assert_called_once_with(
            "KILL QUERY WHERE query_id = 'bound-id'"
        )

    def test_cancel_rejects_non_uuid_query_id(self):
        """A non-UUID query_id must be refused before any KILL QUERY is issued."""
        mock_client = MagicMock()
        client_entry = _ClientCacheEntry(mock_client, 0)
        hostile = "foo'; DROP TABLE x; --"

        with _active_queries_lock:
            _active_queries[hostile] = _ActiveQueryState(
                "SELECT 1", client_entry=client_entry
            )

        _cancel_query(hostile)

        mock_client.command.assert_not_called()
        with _active_queries_lock:
            assert _active_queries[hostile].cancelled is True


class TestRunQueryTimeout:
    """Tests for run_query timeout triggering _cancel_query."""

    @patch("mcp_clickhouse.mcp_server._cancel_query")
    @patch("mcp_clickhouse.mcp_server.QUERY_EXECUTOR")
    def test_timeout_triggers_cancel(self, mock_executor, mock_cancel):
        """When run_query times out, it should call _cancel_query with the query_id."""
        mock_future = MagicMock()
        mock_future.result.side_effect = concurrent.futures.TimeoutError()
        mock_future.cancel.return_value = False
        mock_executor.submit.return_value = mock_future

        with pytest.raises(ToolError, match="timed out"):
            run_query("SELECT sleep(999)")

        # _cancel_query should have been called with the generated query_id
        mock_cancel.assert_called_once()
        query_id = mock_cancel.call_args[0][0]
        assert isinstance(query_id, str)
        assert len(query_id) > 0

    def test_sync_timeout_during_client_acquisition_aborts_query(self):
        client = MagicMock()
        entry = _ClientCacheEntry(client, time.time(), active_users=1)
        acquisition_started = threading.Event()
        release_acquisition = threading.Event()

        def blocked_acquisition(_config):
            acquisition_started.set()
            release_acquisition.wait(timeout=0.5)
            return entry

        with (
            patch("mcp_clickhouse.mcp_server._resolve_client_config", return_value={}),
            patch(
                "mcp_clickhouse.mcp_server.get_mcp_config",
                return_value=SimpleNamespace(query_timeout=0.02),
            ),
            patch(
                "mcp_clickhouse.mcp_server._acquire_clickhouse_client",
                side_effect=blocked_acquisition,
            ),
            patch("mcp_clickhouse.mcp_server._cancel_query_with_bounded_wait"),
        ):
            try:
                with pytest.raises(ToolError, match="timed out"):
                    run_query("SELECT 1")
                assert acquisition_started.is_set()
                with _active_queries_lock:
                    (state,) = _active_queries.values()
                    assert state.cancelled is True
            finally:
                release_acquisition.set()

        for _ in range(50):
            with _active_queries_lock:
                if not _active_queries:
                    break
            time.sleep(0.01)

        client.query.assert_not_called()
        assert entry.active_users == 0
        with _active_queries_lock:
            assert not _active_queries

    def test_timeout_during_validation_aborts_before_query_dispatch(self):
        client = MagicMock()
        client.server_settings = {}
        entry = _ClientCacheEntry(client, time.time(), active_users=1)
        validation_started = threading.Event()
        release_validation = threading.Event()

        def blocked_validation(_query):
            validation_started.set()
            release_validation.wait(timeout=0.5)

        def timeout_config():
            assert validation_started.wait(timeout=1)
            return SimpleNamespace(query_timeout=0.02)

        with (
            patch("mcp_clickhouse.mcp_server._resolve_client_config", return_value={}),
            patch(
                "mcp_clickhouse.mcp_server.get_mcp_config",
                side_effect=timeout_config,
            ),
            patch(
                "mcp_clickhouse.mcp_server._acquire_clickhouse_client",
                return_value=entry,
            ),
            patch(
                "mcp_clickhouse.mcp_server._validate_query_for_destructive_ops",
                side_effect=blocked_validation,
            ),
            patch("mcp_clickhouse.mcp_server._cancel_query_with_bounded_wait"),
        ):
            try:
                with pytest.raises(ToolError, match="timed out"):
                    run_query("SELECT 1")
                assert validation_started.is_set()
                with _active_queries_lock:
                    (state,) = _active_queries.values()
                    assert state.cancelled is True
            finally:
                release_validation.set()

        for _ in range(50):
            with _active_queries_lock:
                if not _active_queries:
                    break
            time.sleep(0.01)

        client.query.assert_not_called()
        assert entry.active_users == 0
        with _active_queries_lock:
            assert not _active_queries

    @pytest.mark.asyncio
    async def test_async_timeout_during_client_acquisition_aborts_query(self):
        client = MagicMock()
        entry = _ClientCacheEntry(client, time.time(), active_users=1)
        acquisition_started = threading.Event()
        release_acquisition = threading.Event()

        def blocked_acquisition(_config):
            acquisition_started.set()
            release_acquisition.wait(timeout=0.5)
            return entry

        with (
            patch("mcp_clickhouse.mcp_server._resolve_client_config", return_value={}),
            patch(
                "mcp_clickhouse.mcp_server.get_mcp_config",
                return_value=SimpleNamespace(query_timeout=0.02),
            ),
            patch(
                "mcp_clickhouse.mcp_server._acquire_clickhouse_client",
                side_effect=blocked_acquisition,
            ),
            patch("mcp_clickhouse.mcp_server._cancel_query_async"),
        ):
            try:
                with pytest.raises(ToolError, match="timed out"):
                    await run_query_async("SELECT 1")
                assert acquisition_started.is_set()
                with _active_queries_lock:
                    (state,) = _active_queries.values()
                    assert state.cancelled is True
            finally:
                release_acquisition.set()

        for _ in range(50):
            with _active_queries_lock:
                if not _active_queries:
                    break
            await asyncio.sleep(0.01)

        client.query.assert_not_called()
        assert entry.active_users == 0
        with _active_queries_lock:
            assert not _active_queries

    @patch("mcp_clickhouse.mcp_server._cancel_query_with_bounded_wait")
    @patch("mcp_clickhouse.mcp_server.QUERY_EXECUTOR")
    def test_queued_timeout_removes_state(self, mock_executor, mock_cancel):
        queued_future = MagicMock()
        queued_future.result.side_effect = concurrent.futures.TimeoutError()
        queued_future.cancel.return_value = True
        mock_executor.submit.return_value = queued_future

        with (
            patch("mcp_clickhouse.mcp_server._resolve_client_config", return_value={}),
            patch(
                "mcp_clickhouse.mcp_server.get_mcp_config",
                return_value=SimpleNamespace(query_timeout=0.02),
            ),
            pytest.raises(ToolError, match="timed out"),
        ):
            run_query("SELECT 1")

        mock_cancel.assert_not_called()
        with _active_queries_lock:
            assert not _active_queries

    @pytest.mark.asyncio
    async def test_async_queued_timeout_removes_state(self):
        queued_future = concurrent.futures.Future()
        with (
            patch("mcp_clickhouse.mcp_server.QUERY_EXECUTOR") as query_executor,
            patch("mcp_clickhouse.mcp_server._resolve_client_config", return_value={}),
            patch(
                "mcp_clickhouse.mcp_server.get_mcp_config",
                return_value=SimpleNamespace(query_timeout=0.01),
            ),
            patch("mcp_clickhouse.mcp_server._cancel_query_async") as cancel_query,
        ):
            query_executor.submit.return_value = queued_future
            with pytest.raises(ToolError, match="timed out"):
                await run_query_async("SELECT 1")

        cancel_query.assert_not_called()
        with _active_queries_lock:
            assert not _active_queries

    @pytest.mark.asyncio
    async def test_async_queued_caller_cancellation_removes_state(self):
        queued_future = concurrent.futures.Future()
        with (
            patch("mcp_clickhouse.mcp_server.QUERY_EXECUTOR") as query_executor,
            patch("mcp_clickhouse.mcp_server._resolve_client_config", return_value={}),
            patch(
                "mcp_clickhouse.mcp_server.get_mcp_config",
                return_value=SimpleNamespace(query_timeout=30),
            ),
            patch("mcp_clickhouse.mcp_server._cancel_query_async") as cancel_query,
        ):
            query_executor.submit.return_value = queued_future
            task = asyncio.create_task(run_query_async("SELECT 1"))
            for _ in range(10):
                with _active_queries_lock:
                    if _active_queries:
                        break
                await asyncio.sleep(0)

            with _active_queries_lock:
                assert len(_active_queries) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert queued_future.cancelled()
        cancel_query.assert_not_awaited()
        with _active_queries_lock:
            assert not _active_queries

    @pytest.mark.parametrize("runner", [run_query, run_query_async])
    @pytest.mark.asyncio
    async def test_submit_failure_removes_state(self, runner):
        with (
            patch("mcp_clickhouse.mcp_server.QUERY_EXECUTOR") as query_executor,
            patch("mcp_clickhouse.mcp_server._resolve_client_config", return_value={}),
        ):
            query_executor.submit.side_effect = RuntimeError("executor closed")
            with pytest.raises(RuntimeError, match="executor closed"):
                result = runner("SELECT 1")
                if asyncio.iscoroutine(result):
                    await result

        with _active_queries_lock:
            assert not _active_queries

    @pytest.mark.asyncio
    async def test_async_timeout_cancellation_is_off_loop_and_bounded(self):
        pending_query = concurrent.futures.Future()
        pending_query.set_running_or_notify_cancel()
        started = threading.Event()
        release = threading.Event()

        def slow_cancel(_query_id):
            started.set()
            release.wait(timeout=0.5)

        with (
            patch("mcp_clickhouse.mcp_server.QUERY_EXECUTOR") as query_executor,
            patch(
                "mcp_clickhouse.mcp_server._resolve_client_config", return_value={}
            ),
            patch(
                "mcp_clickhouse.mcp_server.get_mcp_config",
                return_value=SimpleNamespace(query_timeout=0.01),
            ),
            patch("mcp_clickhouse.mcp_server._cancel_query", side_effect=slow_cancel),
            patch("mcp_clickhouse.mcp_server._QUERY_CANCELLATION_WAIT_SECONDS", 0.08),
        ):
            query_executor.submit.return_value = pending_query
            task = asyncio.create_task(run_query_async("SELECT sleep(999)"))
            try:
                # Waiting for the cancel's start signal on a worker thread proves the
                # loop stayed free: had _cancel_query run inline, the task would
                # already be done by the time control returns here.
                cancel_started = await asyncio.get_running_loop().run_in_executor(
                    None, started.wait, 5
                )
                assert cancel_started
                assert not task.done()
                with pytest.raises(ToolError, match="timed out"):
                    await asyncio.wait_for(task, timeout=5)
            finally:
                release.set()

    @pytest.mark.asyncio
    async def test_queued_async_cancellation_runs_after_bounded_wait(self):
        blocker_started = [threading.Event(), threading.Event()]
        release_blockers = threading.Event()
        cancellation_ran = threading.Event()

        def block_worker(index):
            blocker_started[index].set()
            release_blockers.wait(timeout=1)

        blocker_futures = [
            mcp_server.CANCELLATION_EXECUTOR.submit(block_worker, index)
            for index in range(2)
        ]
        try:
            assert all(started.wait(timeout=1) for started in blocker_started)

            with (
                patch(
                    "mcp_clickhouse.mcp_server._cancel_query",
                    side_effect=lambda _query_id: cancellation_ran.set(),
                ),
                patch("mcp_clickhouse.mcp_server._QUERY_CANCELLATION_WAIT_SECONDS", 0.02),
            ):
                await _cancel_query_async(str(uuid.uuid4()))
                assert not cancellation_ran.is_set()
                release_blockers.set()

                for _ in range(100):
                    if cancellation_ran.is_set():
                        break
                    await asyncio.sleep(0.01)
        finally:
            release_blockers.set()
            for future in blocker_futures:
                future.result(timeout=1)

        assert cancellation_ran.is_set()


class TestRunMetadataTool:
    """Tests for the async wrapper that runs list_databases/list_tables helpers."""

    @pytest.mark.asyncio
    async def test_returns_helper_result(self):
        def helper(prefix, suffix):
            return prefix + suffix

        assert await _run_metadata_tool("list_databases", helper, "a", "b") == "ab"

    @pytest.mark.asyncio
    async def test_timeout_raises_tool_error_and_logs_warning(self):
        release = threading.Event()

        def slow_helper():
            release.wait(timeout=2)
            return "internal-result-detail"

        try:
            with (
                patch(
                    "mcp_clickhouse.mcp_server.get_mcp_config",
                    return_value=SimpleNamespace(query_timeout=0.02),
                ),
                patch("mcp_clickhouse.mcp_server.logger") as mock_logger,
            ):
                with pytest.raises(ToolError) as exc_info:
                    await _run_metadata_tool("list_tables", slow_helper)
        finally:
            release.set()

        message = str(exc_info.value)
        assert message == "list_tables timed out after 0.02 seconds"
        assert "internal-result-detail" not in message
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.args[1:] == ("list_tables", 0.02)

    @pytest.mark.asyncio
    async def test_tool_error_from_helper_propagates_unchanged(self):
        original = ToolError("page_size must be greater than 0")

        def failing_helper():
            raise original

        with pytest.raises(ToolError) as exc_info:
            await _run_metadata_tool("list_tables", failing_helper)

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_generic_exception_from_helper_propagates(self):
        def failing_helper():
            raise ConnectionError("clickhouse unreachable")

        with pytest.raises(ConnectionError, match="clickhouse unreachable"):
            await _run_metadata_tool("list_databases", failing_helper)

    @pytest.mark.asyncio
    async def test_cancelling_the_caller_cancels_a_queued_future(self):
        """Task cancellation reaches the concurrent future through wrap_future.

        Every worker is occupied first so the helper stays queued; a queued
        future can be cancelled, which proves the propagation rather than
        relying on a running helper that cancel() cannot stop.
        """
        release = threading.Event()
        blockers_started = threading.Barrier(mcp_server.QUERY_EXECUTOR._max_workers + 1)
        submitted = {}
        real_submit = mcp_server.QUERY_EXECUTOR.submit

        def blocker():
            blockers_started.wait(timeout=5)
            release.wait(timeout=5)

        def never_runs():
            raise AssertionError("queued helper must not run after cancellation")

        def recording_submit(fn, *args, **kwargs):
            future = real_submit(fn, *args, **kwargs)
            submitted["future"] = future
            return future

        blocker_futures = [
            real_submit(blocker) for _ in range(mcp_server.QUERY_EXECUTOR._max_workers)
        ]
        try:
            # Wait until every worker thread is inside blocker().
            await asyncio.get_running_loop().run_in_executor(None, blockers_started.wait, 5)
            with patch.object(mcp_server.QUERY_EXECUTOR, "submit", side_effect=recording_submit):
                task = asyncio.create_task(_run_metadata_tool("list_databases", never_runs))
                await asyncio.sleep(0)
                assert "future" in submitted
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
        finally:
            release.set()
            concurrent.futures.wait(blocker_futures, timeout=5)

        assert submitted["future"].cancelled() is True


class TestListTablesDeadline:
    """list_tables stops issuing queries once the MCP timeout has passed."""

    @staticmethod
    def _fake_client(table_names):
        client = MagicMock()

        def query(sql):
            if "system.tables" in sql and "name IN" not in sql:
                return SimpleNamespace(column_names=["name"], result_rows=[(n,) for n in table_names])
            raise AssertionError(f"unexpected query after deadline: {sql}")

        client.query.side_effect = query
        return client

    def test_get_paginated_table_data_without_deadline_is_unchanged(self):
        client = MagicMock()
        client.query.return_value = SimpleNamespace(column_names=["database", "name"], result_rows=[])

        tables, end_idx, has_more = mcp_server.get_paginated_table_data(
            client, "db", ["t1"], 0, 50, include_detailed_columns=False
        )

        assert (tables, end_idx, has_more) == ([], 1, False)
        assert client.query.call_count == 1

    def test_get_paginated_table_data_stops_before_the_page_query(self):
        client = MagicMock()

        with pytest.raises(ToolError, match="list_tables timed out"):
            mcp_server.get_paginated_table_data(
                client, "db", ["t1"], 0, 50, deadline=time.monotonic() - 1
            )

        client.query.assert_not_called()

    def test_list_tables_impl_stops_after_the_table_name_query(self):
        client = self._fake_client(["t1", "t2"])

        with pytest.raises(ToolError, match="list_tables timed out"):
            mcp_server._list_tables_impl(
                client, "db", None, None, None, 50, True, deadline=time.monotonic() - 1
            )

        # Only the table-name query ran; the deadline stopped the page query.
        assert client.query.call_count == 1

    def test_deadline_between_column_queries(self):
        """A deadline passing mid-page stops the remaining per-table column queries."""
        client = MagicMock()
        table_columns = [
            "database", "name", "engine", "create_table_query", "dependencies_database",
            "dependencies_table", "engine_full", "sorting_key", "primary_key", "total_rows",
            "total_bytes", "total_bytes_uncompressed", "parts", "active_parts", "total_marks",
            "comment",
        ]
        page = SimpleNamespace(
            column_names=table_columns,
            result_rows=[
                ("db", name, "MergeTree", "", "", "", "", "", "", 0, 0, 0, 0, 0, 0, None)
                for name in ("t1", "t2")
            ],
        )
        columns = SimpleNamespace(column_names=["database", "table", "name"], result_rows=[])
        clock = iter([0.0, 0.0, 10.0])
        client.query.side_effect = [page, columns, columns]

        with patch("mcp_clickhouse.mcp_server.time.monotonic", side_effect=lambda: next(clock)):
            with pytest.raises(ToolError, match="list_tables timed out"):
                mcp_server.get_paginated_table_data(client, "db", ["t1", "t2"], 0, 50, deadline=5.0)

        # Page query and the first table's column query ran; the second did not.
        assert client.query.call_count == 2
