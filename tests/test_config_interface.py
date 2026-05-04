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


def test_server_host_name_configuration(monkeypatch: pytest.MonkeyPatch):
    """Test that server_host_name is included when CLICKHOUSE_SERVER_HOST_NAME is set."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "load-balancer.example.com")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test")
    monkeypatch.setenv("CLICKHOUSE_SERVER_HOST_NAME", "server.example.com")

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["server_host_name"] == "server.example.com"


def test_server_host_name_omitted_when_unset(monkeypatch: pytest.MonkeyPatch):
    """Test that server_host_name is omitted when CLICKHOUSE_SERVER_HOST_NAME is not set."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "load-balancer.example.com")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test")
    monkeypatch.delenv("CLICKHOUSE_SERVER_HOST_NAME", raising=False)

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert "server_host_name" not in client_config


def test_client_cert_configuration(monkeypatch: pytest.MonkeyPatch):
    """Test that client cert and key are passed through when set."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.setenv("CLICKHOUSE_CLIENT_CERT", "/path/to/client.pem")
    monkeypatch.setenv("CLICKHOUSE_CLIENT_CERT_KEY", "/path/to/client.key")
    monkeypatch.delenv("CLICKHOUSE_PASSWORD", raising=False)

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["client_cert"] == "/path/to/client.pem"
    assert client_config["client_cert_key"] == "/path/to/client.key"
    assert client_config["password"] == ""


def test_client_cert_not_in_config_when_unset(monkeypatch: pytest.MonkeyPatch):
    """Test that client cert fields are omitted when not set."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test")
    monkeypatch.delenv("CLICKHOUSE_CLIENT_CERT", raising=False)
    monkeypatch.delenv("CLICKHOUSE_CLIENT_CERT_KEY", raising=False)

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert "client_cert" not in client_config
    assert "client_cert_key" not in client_config


def test_password_required_without_client_cert(monkeypatch: pytest.MonkeyPatch):
    """Test that CLICKHOUSE_PASSWORD is required when no client cert is set."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.delenv("CLICKHOUSE_PASSWORD", raising=False)
    monkeypatch.delenv("CLICKHOUSE_CLIENT_CERT", raising=False)

    with pytest.raises(ValueError, match="CLICKHOUSE_PASSWORD"):
        ClickHouseConfig()
