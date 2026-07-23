"""Tests for ClickHouse connection failure hints."""

from unittest.mock import MagicMock, patch

import pytest
from clickhouse_connect.driver.exceptions import OperationalError

from mcp_clickhouse.mcp_server import (
    _NATIVE_PROTOCOL_PORTS,
    _connection_error_hints,
    _format_connection_failure,
    create_clickhouse_client,
)


def test_native_protocol_ports_constant():
    assert 9000 in _NATIVE_PROTOCOL_PORTS
    assert 9440 in _NATIVE_PROTOCOL_PORTS
    assert 8123 not in _NATIVE_PROTOCOL_PORTS
    assert 8443 not in _NATIVE_PROTOCOL_PORTS


def test_hint_for_native_port_in_config():
    error = Exception("Connection refused")
    config = {"host": "localhost", "port": 9000, "secure": False}

    hints = _connection_error_hints(error, config)

    assert len(hints) == 1
    assert "native TCP" in hints[0]
    assert "8123" in hints[0]
    assert "8443" in hints[0]


def test_hint_for_native_port_server_message():
    error = Exception(
        "HTTP driver received HTTP status 400, server response: "
        "Port 9000 is for clickhouse-client program"
    )
    # User may have mapped something oddly; message still triggers the hint.
    config = {"host": "xxx.us-east-1.aws.clickhouse.cloud", "port": 8443, "secure": True}

    hints = _connection_error_hints(error, config)

    assert len(hints) == 1
    assert "reached native TCP port 9000" in hints[0]
    assert "configured for xxx.us-east-1.aws.clickhouse.cloud:8443" in hints[0]
    assert "Check DNS, service, proxy, load-balancer, and port mappings" in hints[0]
    assert "CLICKHOUSE_PORT=8443 looks like" not in hints[0]


def test_hint_for_tls_mismatch():
    error = Exception("ssl.SSLError: [SSL: WRONG_VERSION_NUMBER] wrong version number")
    config = {"host": "db.example.com", "port": 8443, "secure": False}

    hints = _connection_error_hints(error, config)

    assert len(hints) == 1
    assert "CLICKHOUSE_SECURE=false" in hints[0]
    assert "database connection" in hints[0]
    assert "MCP or ingress" in hints[0]


def test_hint_for_http_status_without_other_signals():
    error = Exception("HTTP driver received HTTP status 400")
    config = {"host": "localhost", "port": 8443, "secure": False}

    hints = _connection_error_hints(error, config)

    assert len(hints) == 1
    assert "CLICKHOUSE_SECURE=false" in hints[0]
    assert "HTTP interface" in hints[0]


def test_connection_refused_hint_starts_with_reachability():
    error = Exception("Connection refused")
    config = {"host": "localhost", "port": 8123, "secure": False}

    hints = _connection_error_hints(error, config)

    assert len(hints) == 1
    assert "running and reachable" in hints[0]
    assert "network or proxy routing" in hints[0]
    assert "CLICKHOUSE_SECURE=false" in hints[0]


def test_no_hint_for_unrelated_errors():
    error = Exception("Authentication failed: password is incorrect")
    config = {"host": "localhost", "port": 8123, "secure": False}

    hints = _connection_error_hints(error, config)

    assert hints == []


def test_format_connection_failure_appends_hints():
    error = Exception("Port 9000 is for clickhouse-client program")
    config = {"host": "localhost", "port": 9000, "secure": False}

    message = _format_connection_failure(error, config)

    assert message.startswith("Failed to connect to ClickHouse:")
    assert "Hint:" in message
    assert "HTTP interface" in message


def test_format_connection_failure_without_hints():
    error = Exception("Authentication failed")
    config = {"host": "localhost", "port": 8123, "secure": False}

    message = _format_connection_failure(error, config)

    assert message == "Failed to connect to ClickHouse: Authentication failed"
    assert "Hint:" not in message


@patch("mcp_clickhouse.mcp_server.clickhouse_connect")
def test_create_client_preserves_exception_and_logs_hint(mock_cc, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "secret")
    monkeypatch.setenv("CLICKHOUSE_PORT", "8123")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")

    # Reset config singleton so env changes are picked up
    import mcp_clickhouse.mcp_env as mcp_env

    mcp_env._CONFIG_INSTANCE = None

    original_error = OperationalError(
        "HTTP driver received HTTP status 400, server response: "
        "Port 9000 is for clickhouse-client program"
    )
    mock_cc.get_client.side_effect = original_error

    with caplog.at_level(logging.ERROR, logger="mcp-clickhouse"):
        with pytest.raises(OperationalError) as exc_info:
            create_clickhouse_client()

    assert exc_info.value is original_error
    log_message = "\n".join(record.message for record in caplog.records)
    assert "Failed to connect to ClickHouse" in log_message
    assert "Hint:" in log_message
    assert "reached native TCP port 9000" in log_message

    # Clean up singleton for other tests
    mcp_env._CONFIG_INSTANCE = None


@patch("mcp_clickhouse.mcp_server.clickhouse_connect")
def test_create_client_warns_on_native_port(mock_cc, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "secret")
    monkeypatch.setenv("CLICKHOUSE_PORT", "9000")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")

    # Reset config singleton so env changes are picked up
    import mcp_clickhouse.mcp_env as mcp_env

    mcp_env._CONFIG_INSTANCE = None

    mock_cc.get_client.return_value = MagicMock(server_version="24.1")

    with caplog.at_level(logging.WARNING, logger="mcp-clickhouse"):
        create_clickhouse_client()

    assert any("native TCP protocol port" in record.message for record in caplog.records)

    # Clean up singleton for other tests
    mcp_env._CONFIG_INSTANCE = None
