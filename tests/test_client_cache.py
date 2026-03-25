"""Tests for ClickHouse client caching and reuse."""

import time
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from mcp_clickhouse.mcp_env import get_mcp_config
from mcp_clickhouse.mcp_server import (
    _active_queries,
    _active_queries_lock,
    _clear_client_cache,
    _client_cache,
    _client_cache_lock,
    _config_to_cache_key,
    _resolve_client_config,
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


class TestClientCaching:
    """Tests for client cache behavior."""

    def setup_method(self):
        _clear_client_cache()

    def teardown_method(self):
        _clear_client_cache()

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_same_config_returns_cached_client(self, _mock_ctx, mock_cc):
        """Same config should return the cached client without creating a new one."""
        mock_client = MagicMock(server_version="24.1")
        mock_cc.get_client.return_value = mock_client

        client1 = create_clickhouse_client()
        client2 = create_clickhouse_client()

        assert client1 is client2
        # get_client should only be called once
        assert mock_cc.get_client.call_count == 1

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context")
    def test_different_config_creates_new_client(self, mock_get_context, mock_cc):
        """Different session configs should produce different cached clients."""
        mock_client_a = MagicMock(server_version="24.1")
        mock_client_b = MagicMock(server_version="24.1")
        mock_cc.get_client.side_effect = [mock_client_a, mock_client_b]

        # First call: no overrides
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = None
        mock_get_context.return_value = mock_ctx
        client1 = create_clickhouse_client()

        _clear_client_cache()

        # Second call: with override that changes the config key
        mock_ctx2 = MagicMock()
        mock_ctx2.get_state.return_value = {"connect_timeout": 99}
        mock_get_context.return_value = mock_ctx2
        client2 = create_clickhouse_client()

        assert client1 is not client2
        assert mock_cc.get_client.call_count == 2

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_stale_client_evicted_on_ping_failure(self, _mock_ctx, mock_cc):
        """Client that fails ping after idle should be evicted and recreated."""
        mock_client_old = MagicMock(server_version="24.1")
        mock_client_old.ping.return_value = False
        mock_client_new = MagicMock(server_version="24.2")
        mock_cc.get_client.side_effect = [mock_client_old, mock_client_new]

        client1 = create_clickhouse_client()
        assert client1 is mock_client_old

        # Simulate idle time exceeding threshold
        with _client_cache_lock:
            for key, val in _client_cache.items():
                client, _ = val
                _client_cache[key] = (client, time.time() - 120)

        client2 = create_clickhouse_client()
        assert client2 is mock_client_new
        assert mock_cc.get_client.call_count == 2

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_autogenerate_session_id_disabled(self, _mock_ctx, mock_cc):
        """Cached clients should be created with autogenerate_session_id=False."""
        mock_cc.get_client.return_value = MagicMock(server_version="24.1")

        create_clickhouse_client()

        call_kwargs = mock_cc.get_client.call_args[1]
        assert call_kwargs["autogenerate_session_id"] is False

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_clear_cache_closes_clients(self, _mock_ctx, mock_cc):
        """_clear_client_cache should close all cached clients."""
        mock_client = MagicMock(server_version="24.1")
        mock_cc.get_client.return_value = mock_client

        create_clickhouse_client()
        _clear_client_cache()

        mock_client.close.assert_called_once()


class TestResolveClientConfig:
    """Tests for _resolve_client_config."""

    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_send_receive_timeout_capped_when_not_explicit(self, _mock_ctx):
        """send_receive_timeout should be capped to query_timeout + 5 by default."""
        config = _resolve_client_config()

        expected = get_mcp_config().query_timeout + 5
        assert config["send_receive_timeout"] == expected

    @patch.dict("os.environ", {"CLICKHOUSE_SEND_RECEIVE_TIMEOUT": "200"})
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_send_receive_timeout_not_capped_when_explicit(self, _mock_ctx):
        """Explicit env var should bypass the auto-cap."""
        config = _resolve_client_config()
        assert config["send_receive_timeout"] == 200

    @patch("mcp_clickhouse.mcp_server.get_context")
    def test_session_override_timeout_not_capped(self, mock_get_context):
        """Session override of send_receive_timeout should bypass the auto-cap."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = {"send_receive_timeout": 300}
        mock_get_context.return_value = mock_ctx

        config = _resolve_client_config()
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
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_execute_query_evicts_on_connection_error(self, _mock_ctx, mock_cc):
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
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_execute_query_no_evict_on_sql_error(self, _mock_ctx, mock_cc):
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


class TestPingExceptionHandling:
    """Tests for ping exception handling in create_clickhouse_client."""

    def setup_method(self):
        _clear_client_cache()

    def teardown_method(self):
        _clear_client_cache()

    @patch("mcp_clickhouse.mcp_server.clickhouse_connect")
    @patch("mcp_clickhouse.mcp_server.get_context", side_effect=RuntimeError)
    def test_ping_exception_evicts_and_recreates(self, _mock_ctx, mock_cc):
        """A ping() that raises should evict the client and create a new one."""
        mock_client_old = MagicMock(server_version="24.1")
        mock_client_old.ping.side_effect = Exception("boom")
        mock_client_new = MagicMock(server_version="24.2")
        mock_cc.get_client.side_effect = [mock_client_old, mock_client_new]

        client1 = create_clickhouse_client()
        assert client1 is mock_client_old

        # Simulate idle time exceeding threshold
        with _client_cache_lock:
            for key, val in _client_cache.items():
                client, _ = val
                _client_cache[key] = (client, time.time() - 120)

        client2 = create_clickhouse_client()
        assert client2 is mock_client_new
        assert mock_cc.get_client.call_count == 2
