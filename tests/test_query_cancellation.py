"""Tests for query ID tracking and server-side cancellation."""

import concurrent.futures
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from mcp_clickhouse.mcp_server import (
    _active_queries,
    _active_queries_lock,
    _cancel_query,
    _clear_client_cache,
    _client_cache,
    _client_cache_lock,
    _resolve_client_config,
    execute_query,
    run_query,
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
        cache_key = (("host", "localhost"), ("port", 8443))
        query_id = str(uuid.uuid4())

        # Set up cached client and active query
        with _client_cache_lock:
            _client_cache[cache_key] = (mock_client, 0)
        with _active_queries_lock:
            _active_queries[query_id] = (cache_key, "SELECT sleep(60)")

        _cancel_query(query_id)

        mock_client.command.assert_called_once_with(
            f"KILL QUERY WHERE query_id = '{query_id}'"
        )
        # Should be removed from active queries
        with _active_queries_lock:
            assert query_id not in _active_queries

    def test_cancel_noop_for_completed_query(self):
        """_cancel_query should be a no-op if the query already completed."""
        # No entry in _active_queries
        _cancel_query(str(uuid.uuid4()))  # Should not raise

    def test_cancel_warns_without_cached_client(self):
        """_cancel_query should log warning if no cached client is available."""
        cache_key = (("host", "gone"),)
        query_id = str(uuid.uuid4())
        with _active_queries_lock:
            _active_queries[query_id] = (cache_key, "SELECT 1")

        # No client in cache for this key
        _cancel_query(query_id)  # Should not raise

        with _active_queries_lock:
            assert query_id not in _active_queries

    def test_cancel_failure_does_not_raise(self):
        """_cancel_query should swallow exceptions from KILL QUERY."""
        mock_client = MagicMock()
        mock_client.command.side_effect = Exception("Permission denied")
        cache_key = (("host", "localhost"),)
        query_id = str(uuid.uuid4())

        with _client_cache_lock:
            _client_cache[cache_key] = (mock_client, 0)
        with _active_queries_lock:
            _active_queries[query_id] = (cache_key, "SELECT 1")

        _cancel_query(query_id)  # Should not raise

    def test_cancel_rejects_non_uuid_query_id(self):
        """A non-UUID query_id must be refused before any KILL QUERY is issued."""
        mock_client = MagicMock()
        cache_key = (("host", "localhost"),)
        hostile = "foo'; DROP TABLE x; --"

        with _client_cache_lock:
            _client_cache[cache_key] = (mock_client, 0)
        with _active_queries_lock:
            _active_queries[hostile] = (cache_key, "SELECT 1")

        _cancel_query(hostile)

        mock_client.command.assert_not_called()
        # The active-query entry is always popped first, so it's gone either way.
        with _active_queries_lock:
            assert hostile not in _active_queries


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
        mock_executor.submit.return_value = mock_future

        with pytest.raises(ToolError, match="timed out"):
            run_query("SELECT sleep(999)")

        # _cancel_query should have been called with the generated query_id
        mock_cancel.assert_called_once()
        query_id = mock_cancel.call_args[0][0]
        assert isinstance(query_id, str)
        assert len(query_id) > 0
