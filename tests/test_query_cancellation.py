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

from mcp_clickhouse.mcp_server import (
    _ActiveQueryState,
    _ClientCacheEntry,
    _active_queries,
    _active_queries_lock,
    _cancel_query,
    _clear_client_cache,
    _resolve_client_config,
    execute_query,
    run_query,
    run_query_async,
)


class TestQueryIdTracking:
    """Tests for query_id propagation through execute_query."""

    def setup_method(self):
        _clear_client_cache()
        with _active_queries_lock:
            _active_queries.clear()

    def teardown_method(self):
        _clear_client_cache()
        with _active_queries_lock:
            _active_queries.clear()

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_query_id_passed_in_settings(self, _mock_ctx, mock_cc):
        """query_id should be included in the settings dict passed to client.query()."""
        mock_client = MagicMock(server_version="24.1")
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
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_active_queries_tracked_and_cleaned(self, _mock_ctx, mock_cc):
        """execute_query should register in _active_queries and clean up on completion."""
        mock_client = MagicMock(server_version="24.1")
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
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_active_queries_cleaned_on_error(self, _mock_ctx, mock_cc):
        """execute_query should clean up _active_queries even on error."""
        mock_client = MagicMock(server_version="24.1")
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

    def setup_method(self):
        _clear_client_cache()
        with _active_queries_lock:
            _active_queries.clear()

    def teardown_method(self):
        _clear_client_cache()
        with _active_queries_lock:
            _active_queries.clear()

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

    def setup_method(self):
        _clear_client_cache()
        with _active_queries_lock:
            _active_queries.clear()

    def teardown_method(self):
        _clear_client_cache()
        with _active_queries_lock:
            _active_queries.clear()

    @patch("mcp_clickhouse.mcp_server._cancel_query")
    @patch("mcp_clickhouse.mcp_server.QUERY_EXECUTOR")
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_timeout_triggers_cancel(self, _mock_ctx, mock_executor, mock_cancel):
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
            started_at = time.monotonic()
            task = asyncio.create_task(run_query_async("SELECT sleep(999)"))
            try:
                await asyncio.sleep(0.04)
                assert time.monotonic() - started_at < 0.15
                assert started.is_set()
                assert not task.done()
                with pytest.raises(ToolError, match="timed out"):
                    await asyncio.wait_for(task, timeout=0.2)
            finally:
                release.set()
