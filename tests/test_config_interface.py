import pytest

from mcp_clickhouse.mcp_env import ChDBConfig, ClickHouseConfig


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


# --- ChDBConfig: chDB-only security-baseline settings ------------------------


def test_chdb_allow_write_access_defaults_false(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHDB_ALLOW_WRITE_ACCESS", raising=False)
    assert ChDBConfig().allow_write_access is False


def test_chdb_allow_write_access_true(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHDB_ALLOW_WRITE_ACCESS", "true")
    assert ChDBConfig().allow_write_access is True


def test_chdb_max_result_bytes_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHDB_MAX_RESULT_BYTES", raising=False)
    assert ChDBConfig().max_result_bytes == 1024 * 1024


def test_chdb_max_result_bytes_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHDB_MAX_RESULT_BYTES", "2048")
    assert ChDBConfig().max_result_bytes == 2048


@pytest.mark.parametrize("bad", ["0", "-5", "notanint", ""])
def test_chdb_max_result_bytes_falls_back_on_invalid(bad, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHDB_MAX_RESULT_BYTES", bad)
    assert ChDBConfig().max_result_bytes == 1024 * 1024


def test_chdb_file_allowlist_empty_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHDB_FILE_ALLOWLIST", raising=False)
    assert ChDBConfig().file_allowlist == ()


def test_chdb_file_allowlist_parses_colon_separated(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHDB_FILE_ALLOWLIST", "/data: /tmp/x :")
    assert ChDBConfig().file_allowlist == ("/data", "/tmp/x")
