import pytest

from mcp_clickhouse.mcp_env import ClickHouseConfig


def test_interface_http_when_secure_false(monkeypatch: pytest.MonkeyPatch):
    """Test that interface is set to 'http' when CLICKHOUSE_SECURE=false."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_PORT", "8123")

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "http"
    assert client_config["secure"] is False
    assert client_config["port"] == 8123


def test_interface_https_when_secure_true(monkeypatch: pytest.MonkeyPatch):
    """Test that interface is set to 'https' when CLICKHOUSE_SECURE=true."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "example.com")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "true")
    monkeypatch.setenv("CLICKHOUSE_PORT", "8443")

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "https"
    assert client_config["secure"] is True
    assert client_config["port"] == 8443


def test_interface_https_by_default(monkeypatch: pytest.MonkeyPatch):
    """Test that interface defaults to 'https' when CLICKHOUSE_SECURE is not set."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "example.com")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test")
    monkeypatch.delenv("CLICKHOUSE_SECURE", raising=False)
    monkeypatch.delenv("CLICKHOUSE_PORT", raising=False)

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "https"
    assert client_config["secure"] is True
    assert client_config["port"] == 8443


def test_interface_http_with_custom_port(monkeypatch: pytest.MonkeyPatch):
    """Test that interface is 'http' with custom port when CLICKHOUSE_SECURE=false."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_PORT", "9000")

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "http"
    assert client_config["secure"] is False
    assert client_config["port"] == 9000


def test_interface_https_with_custom_port(monkeypatch: pytest.MonkeyPatch):
    """Test that interface is 'https' with custom port when CLICKHOUSE_SECURE=true."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "example.com")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "true")
    monkeypatch.setenv("CLICKHOUSE_PORT", "9443")

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "https"
    assert client_config["secure"] is True
    assert client_config["port"] == 9443


def test_role_configuration(monkeypatch: pytest.MonkeyPatch):
    """Test that role is correctly configured when CLICKHOUSE_ROLE is set."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test")
    monkeypatch.setenv("CLICKHOUSE_ROLE", "analytics_reader")

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["settings"]["role"] == "analytics_reader"
