"""Tests for ClickHouse client caching and reuse."""

import time
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from mcp_clickhouse.mcp_env import get_mcp_config
from mcp_clickhouse.mcp_server import (
    _ClientCacheEntry,
    _acquire_clickhouse_client,
    _active_queries,
    _active_queries_lock,
    _clear_client_cache,
    _client_cache,
    _client_cache_lock,
    _config_to_cache_key,
    _evict_cached_client,
    _release_client_entry,
    _resolve_client_config,
    _shutdown,
    create_clickhouse_client,
    execute_query,
)


class TestConfigToCacheKey:
    """Tests for the _config_to_cache_key helper."""

    def test_deterministic_key(self):
        config = {"host": "localhost", "port": 8443, "username": "default"}
        assert _config_to_cache_key(config) == _config_to_cache_key(config)

    def test_order_independent(self):
        config_a = {"host": "localhost", "port": 8443}
        config_b = {"port": 8443, "host": "localhost"}
        assert _config_to_cache_key(config_a) == _config_to_cache_key(config_b)

    def test_nested_dict(self):
        config = {"host": "localhost", "settings": {"role": "admin", "readonly": "1"}}
        key = _config_to_cache_key(config)
        assert isinstance(key, tuple)
        # Nested dict should also be a tuple
        for k, v in key:
            if k == "settings":
                assert isinstance(v, tuple)

    def test_different_configs_different_keys(self):
        config_a = {"host": "host1", "port": 8443}
        config_b = {"host": "host2", "port": 8443}
        assert _config_to_cache_key(config_a) != _config_to_cache_key(config_b)

    def test_unhashable_opaque_value_is_not_cacheable(self):
        class UnhashableOpaqueValue:
            __hash__ = None

        assert _config_to_cache_key({"pool_mgr": UnhashableOpaqueValue()}) is None


class TestClientCaching:
    """Tests for client cache behavior."""

    def setup_method(self):
        _clear_client_cache()

    def teardown_method(self):
        _clear_client_cache()

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_same_config_reuses_cached_client(self, mock_cc):
        """Same config should reuse the internal cached client."""
        mock_client = MagicMock(server_version="24.1")
        mock_cc.get_client.return_value = mock_client
        config = _resolve_client_config()

        entry1 = _acquire_clickhouse_client(config)
        _release_client_entry(entry1)
        entry2 = _acquire_clickhouse_client(config)
        _release_client_entry(entry2)

        assert entry1.client is entry2.client
        assert mock_cc.get_client.call_count == 1

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_different_config_creates_new_client(self, mock_cc):
        """Different request configs should produce different cached clients."""
        mock_client_a = MagicMock(server_version="24.1")
        mock_client_b = MagicMock(server_version="24.1")
        mock_cc.get_client.side_effect = [mock_client_a, mock_client_b]

        # First call: no overrides
        config1 = _resolve_client_config(None)
        entry1 = _acquire_clickhouse_client(config1)
        _release_client_entry(entry1)

        # Second call: with override that changes the config key
        config2 = _resolve_client_config({"connect_timeout": 99})
        entry2 = _acquire_clickhouse_client(config2)
        _release_client_entry(entry2)

        assert entry1.client is not entry2.client
        assert mock_cc.get_client.call_count == 2

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_stale_client_evicted_on_ping_failure(self, mock_cc):
        """Client that fails ping after idle should be evicted and recreated."""
        mock_client_old = MagicMock(server_version="24.1")
        mock_client_old.ping.return_value = False
        mock_client_new = MagicMock(server_version="24.2")
        mock_cc.get_client.side_effect = [mock_client_old, mock_client_new]

        config = _resolve_client_config()
        entry1 = _acquire_clickhouse_client(config)
        assert entry1.client is mock_client_old
        _release_client_entry(entry1)

        # Simulate idle time exceeding threshold
        with _client_cache_lock:
            for entry in _client_cache.values():
                entry.last_used = time.time() - 120

        entry2 = _acquire_clickhouse_client(config)
        assert entry2.client is mock_client_new
        _release_client_entry(entry2)
        assert mock_cc.get_client.call_count == 2

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_autogenerate_session_id_disabled(self, mock_cc):
        """Cached clients should be created with autogenerate_session_id=False."""
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        entry = _acquire_clickhouse_client(_resolve_client_config())
        _release_client_entry(entry)

        call_kwargs = mock_cc.get_client.call_args[1]
        assert call_kwargs["autogenerate_session_id"] is False

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_unhashable_opaque_values_bypass_cache(self, mock_cc):
        class UnhashableOpaqueValue:
            __hash__ = None

        clients = [
            MagicMock(server_version="24.1"),
            MagicMock(server_version="24.2"),
        ]
        mock_cc.get_client.side_effect = clients

        with patch("mcp_clickhouse.mcp_server.id", return_value=1, create=True):
            first = _acquire_clickhouse_client(
                _resolve_client_config({"pool_mgr": UnhashableOpaqueValue()})
            )
            _release_client_entry(first)
            second = _acquire_clickhouse_client(
                _resolve_client_config({"pool_mgr": UnhashableOpaqueValue()})
            )
            _release_client_entry(second)

        assert first.client is clients[0]
        assert second.client is clients[1]
        assert len(_client_cache) == 0
        clients[0].close.assert_called_once_with()
        clients[1].close.assert_called_once_with()

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_clear_cache_closes_clients(self, mock_cc):
        """_clear_client_cache should close all cached clients."""
        mock_client = MagicMock(server_version="24.1")
        mock_cc.get_client.return_value = mock_client

        entry = _acquire_clickhouse_client(_resolve_client_config())
        _release_client_entry(entry)
        _clear_client_cache()

        mock_client.close.assert_called_once()

    @patch("mcp_clickhouse.mcp_server._CLIENT_CACHE_MAXSIZE", 2)
    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_cache_cardinality_is_bounded_and_lru_client_is_closed(self, mock_cc):
        clients = [MagicMock(server_version=f"24.{index}") for index in range(3)]
        mock_cc.get_client.side_effect = clients

        for timeout in (31, 32, 33):
            config = _resolve_client_config({"connect_timeout": timeout})
            entry = _acquire_clickhouse_client(config)
            _release_client_entry(entry)

        assert len(_client_cache) == 2
        clients[0].close.assert_called_once_with()
        clients[1].close.assert_not_called()
        clients[2].close.assert_not_called()

    @patch("mcp_clickhouse.mcp_server._CLIENT_CACHE_MAXSIZE", 2)
    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_lru_eviction_waits_for_in_use_client_release(self, mock_cc):
        clients = [MagicMock(server_version=f"24.{index}") for index in range(3)]
        mock_cc.get_client.side_effect = clients
        first_config = _resolve_client_config({"connect_timeout": 31})
        first_entry = _acquire_clickhouse_client(first_config)

        for timeout in (32, 33):
            config = _resolve_client_config({"connect_timeout": timeout})
            entry = _acquire_clickhouse_client(config)
            _release_client_entry(entry)

        assert first_entry.retired is True
        clients[0].close.assert_not_called()

        _release_client_entry(first_entry)
        clients[0].close.assert_called_once_with()


class TestResolveClientConfig:
    """Tests for _resolve_client_config."""

    def test_send_receive_timeout_capped_when_not_explicit(self):
        """send_receive_timeout should be capped to query_timeout + 5 by default."""
        config = _resolve_client_config()

        expected = get_mcp_config().query_timeout + 5
        assert config["send_receive_timeout"] == expected

    @patch.dict("os.environ", {"CLICKHOUSE_SEND_RECEIVE_TIMEOUT": "200"})
    def test_send_receive_timeout_not_capped_when_explicit(self):
        """Explicit env var should bypass the auto-cap."""
        config = _resolve_client_config()
        assert config["send_receive_timeout"] == 200

    def test_session_override_timeout_not_capped(self):
        """Request override of send_receive_timeout should bypass the auto-cap."""
        config = _resolve_client_config({"send_receive_timeout": 300})
        assert config["send_receive_timeout"] == 300


class TestEvictionOnError:
    """Tests for client eviction on connection errors."""

    def setup_method(self):
        _clear_client_cache()
        with _active_queries_lock:
            _active_queries.clear()

    def teardown_method(self):
        _clear_client_cache()
        with _active_queries_lock:
            _active_queries.clear()

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_execute_query_evicts_on_connection_error(self, mock_cc):
        """execute_query should evict the cached client on connection errors."""
        mock_client = MagicMock(server_version="24.1")
        mock_client.server_settings = {}
        mock_client.query.side_effect = ConnectionError("connection reset")
        mock_cc.get_client.return_value = mock_client

        config = _resolve_client_config()

        with pytest.raises(ToolError, match="connection reset"):
            execute_query("SELECT 1", "evict-test", config)

        # Client should have been evicted — next call creates a new one
        mock_client_new = MagicMock(server_version="24.2")
        mock_client_new.server_settings = {}
        mock_result = MagicMock()
        mock_result.result_rows = []
        mock_result.column_names = []
        mock_client_new.query.return_value = mock_result
        mock_cc.get_client.return_value = mock_client_new

        execute_query("SELECT 1", "evict-test-2", config)
        assert mock_cc.get_client.call_count == 2

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_execute_query_no_evict_on_sql_error(self, mock_cc):
        """execute_query should NOT evict on normal SQL errors (not connection)."""
        mock_client = MagicMock(server_version="24.1")
        mock_client.server_settings = {}
        mock_client.query.side_effect = Exception("Unknown column 'x'")
        mock_cc.get_client.return_value = mock_client

        config = _resolve_client_config()

        with pytest.raises(ToolError):
            execute_query("SELECT x", "no-evict-test", config)

        # Client should still be cached, second call reuses it
        mock_client.query.side_effect = None
        mock_result = MagicMock()
        mock_result.result_rows = []
        mock_result.column_names = []
        mock_client.query.return_value = mock_result
        execute_query("SELECT 1", "no-evict-test-2", config)

        # get_client only called once, reused from cache
        assert mock_cc.get_client.call_count == 1

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_late_error_does_not_evict_concurrent_replacement(self, mock_cc):
        failed_client = MagicMock(server_version="24.1")
        replacement = MagicMock(server_version="24.2")
        mock_cc.get_client.side_effect = [failed_client, replacement]
        config = _resolve_client_config({"connect_timeout": 41})
        failed_entry = _acquire_clickhouse_client(config)

        assert _evict_cached_client(config, failed_client) is True
        assert failed_client.close.call_count == 0
        replacement_entry = _acquire_clickhouse_client(config)
        assert replacement_entry.client is replacement
        _release_client_entry(replacement_entry)

        assert _evict_cached_client(config, failed_client) is False
        with _client_cache_lock:
            cached_entry = _client_cache[_config_to_cache_key(config)]
        assert cached_entry.client is replacement
        replacement.close.assert_not_called()

        _release_client_entry(failed_entry)
        failed_client.close.assert_called_once_with()


class TestPingExceptionHandling:
    """Tests for ping exception handling in the internal client cache."""

    def setup_method(self):
        _clear_client_cache()

    def teardown_method(self):
        _clear_client_cache()

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_ping_exception_evicts_and_recreates(self, mock_cc):
        """A ping() that raises should evict the client and create a new one."""
        mock_client_old = MagicMock(server_version="24.1")
        mock_client_old.ping.side_effect = Exception("boom")
        mock_client_new = MagicMock(server_version="24.2")
        mock_cc.get_client.side_effect = [mock_client_old, mock_client_new]

        config = _resolve_client_config()
        entry1 = _acquire_clickhouse_client(config)
        assert entry1.client is mock_client_old
        _release_client_entry(entry1)

        # Simulate idle time exceeding threshold
        with _client_cache_lock:
            for entry in _client_cache.values():
                entry.last_used = time.time() - 120

        entry2 = _acquire_clickhouse_client(config)
        assert entry2.client is mock_client_new
        _release_client_entry(entry2)
        assert mock_cc.get_client.call_count == 2


class TestCacheRaceHandling:
    """Tests for identity-checked cache updates around the idle-ping path."""

    def setup_method(self):
        _clear_client_cache()

    def teardown_method(self):
        _clear_client_cache()

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_ping_ok_yields_to_newer_cached_client(self, mock_cc):
        """If another thread replaced the cached client during ping,
        a successful ping must not resurrect our stale candidate."""
        stale = MagicMock(server_version="24.1", name="stale")
        replacement = MagicMock(server_version="24.2", name="replacement")
        mock_cc.get_client.return_value = stale

        config = _resolve_client_config()
        entry = _acquire_clickhouse_client(config)
        _release_client_entry(entry)
        with _client_cache_lock:
            (key,) = list(_client_cache.keys())
            stale_entry = _client_cache[key]
            stale_entry.last_used = time.time() - 120

        # While pinging, simulate another thread replacing the entry.
        def ping_and_replace():
            with _client_cache_lock:
                stale_entry.retired = True
                _client_cache[key] = _ClientCacheEntry(replacement, time.time())
            return True

        stale.ping.side_effect = ping_and_replace

        result = _acquire_clickhouse_client(config)

        assert result.client is replacement
        _release_client_entry(result)
        # Stale candidate must be closed; replacement must still be cached.
        stale.close.assert_called_once()
        with _client_cache_lock:
            cached_entry = _client_cache[key]
        assert cached_entry.client is replacement

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_ping_fail_preserves_newer_cached_client(self, mock_cc):
        """A failed ping must not drop a replacement installed by another thread."""
        stale = MagicMock(server_version="24.1", name="stale")
        replacement = MagicMock(server_version="24.2", name="replacement")
        mock_cc.get_client.return_value = stale

        config = _resolve_client_config()
        entry = _acquire_clickhouse_client(config)
        _release_client_entry(entry)
        with _client_cache_lock:
            (key,) = list(_client_cache.keys())
            stale_entry = _client_cache[key]
            stale_entry.last_used = time.time() - 120

        def ping_and_replace():
            with _client_cache_lock:
                stale_entry.retired = True
                _client_cache[key] = _ClientCacheEntry(replacement, time.time())
            return False  # ping fails

        stale.ping.side_effect = ping_and_replace

        result = _acquire_clickhouse_client(config)

        assert result.client is replacement
        _release_client_entry(result)
        stale.close.assert_called_once()
        assert mock_cc.get_client.call_count == 1
        with _client_cache_lock:
            cached_entry = _client_cache[key]
        assert cached_entry.client is replacement


class TestCompatibilityClientLifetime:
    """Tests for the exported raw client helper."""

    def setup_method(self):
        _clear_client_cache()

    def teardown_method(self):
        _clear_client_cache()

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_default_session_id_behavior_is_preserved(self, mock_cc):
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client()

        assert "autogenerate_session_id" not in mock_cc.get_client.call_args.kwargs

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_explicit_session_id_behavior_is_preserved(self, mock_cc):
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client({"autogenerate_session_id": True})

        assert mock_cc.get_client.call_args.kwargs["autogenerate_session_id"] is True

    @patch("mcp_clickhouse.mcp_server._CLIENT_CACHE_MAXSIZE", 2)
    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    def test_retained_client_survives_internal_cache_churn(self, mock_cc):
        retained = MagicMock(server_version="24.0")
        cached_clients = [
            MagicMock(server_version=f"24.{index}") for index in range(1, 4)
        ]
        mock_cc.get_client.side_effect = [retained, *cached_clients]

        raw_client = create_clickhouse_client({"connect_timeout": 30})
        for timeout in (31, 32, 33):
            config = _resolve_client_config({"connect_timeout": timeout})
            entry = _acquire_clickhouse_client(config)
            _release_client_entry(entry)

        assert raw_client is retained
        assert len(_client_cache) == 2
        assert all(entry.client is not retained for entry in _client_cache.values())
        retained.close.assert_not_called()
        retained.command("SELECT 1")
        retained.command.assert_called_once_with("SELECT 1")

        retained.close()


class TestShutdownOrdering:
    """Tests that atexit shutdown closes the executor before the cache."""

    @patch("mcp_clickhouse.mcp_server._clear_client_cache")
    @patch("mcp_clickhouse.mcp_server.HEALTH_EXECUTOR")
    @patch("mcp_clickhouse.mcp_server.CANCELLATION_EXECUTOR")
    @patch("mcp_clickhouse.mcp_server.QUERY_EXECUTOR")
    def test_executor_shutdown_runs_before_cache_clear(
        self, mock_query_executor, mock_cancellation_executor, mock_health_executor, mock_clear
    ):
        """Shutdown drains all executors before clearing the client cache."""
        call_order = []
        mock_query_executor.shutdown.side_effect = lambda wait: call_order.append("query")
        mock_cancellation_executor.shutdown.side_effect = lambda wait: call_order.append(
            "cancel"
        )
        mock_health_executor.shutdown.side_effect = lambda wait: call_order.append("health")
        mock_clear.side_effect = lambda: call_order.append("cache")

        _shutdown()

        assert call_order == ["query", "cancel", "health", "cache"]
        mock_query_executor.shutdown.assert_called_once_with(wait=True)
        mock_cancellation_executor.shutdown.assert_called_once_with(wait=True)
        mock_health_executor.shutdown.assert_called_once_with(wait=True)
        mock_clear.assert_called_once_with()
