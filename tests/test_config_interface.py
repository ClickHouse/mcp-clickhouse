import os

from mcp_clickhouse.mcp_env import ClickHouseConfig


def test_interface_http_when_secure_false():
    """Test that interface is set to 'http' when CLICKHOUSE_SECURE=false."""
    os.environ["CLICKHOUSE_HOST"] = "localhost"
    os.environ["CLICKHOUSE_USER"] = "test"
    os.environ["CLICKHOUSE_PASSWORD"] = "test"
    os.environ["CLICKHOUSE_SECURE"] = "false"
    os.environ["CLICKHOUSE_PORT"] = "8123"

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "http"
    assert client_config["secure"] is False
    assert client_config["port"] == 8123


def test_interface_https_when_secure_true():
    """Test that interface is set to 'https' when CLICKHOUSE_SECURE=true."""
    os.environ["CLICKHOUSE_HOST"] = "example.com"
    os.environ["CLICKHOUSE_USER"] = "test"
    os.environ["CLICKHOUSE_PASSWORD"] = "test"
    os.environ["CLICKHOUSE_SECURE"] = "true"
    os.environ["CLICKHOUSE_PORT"] = "8443"

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "https"
    assert client_config["secure"] is True
    assert client_config["port"] == 8443


def test_interface_https_by_default():
    """Test that interface defaults to 'https' when CLICKHOUSE_SECURE is not set."""
    os.environ["CLICKHOUSE_HOST"] = "example.com"
    os.environ["CLICKHOUSE_USER"] = "test"
    os.environ["CLICKHOUSE_PASSWORD"] = "test"
    if "CLICKHOUSE_SECURE" in os.environ:
        del os.environ["CLICKHOUSE_SECURE"]
    if "CLICKHOUSE_PORT" in os.environ:
        del os.environ["CLICKHOUSE_PORT"]

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "https"
    assert client_config["secure"] is True
    assert client_config["port"] == 8443


def test_interface_http_with_custom_port():
    """Test that interface is 'http' with custom port when CLICKHOUSE_SECURE=false."""
    os.environ["CLICKHOUSE_HOST"] = "localhost"
    os.environ["CLICKHOUSE_USER"] = "test"
    os.environ["CLICKHOUSE_PASSWORD"] = "test"
    os.environ["CLICKHOUSE_SECURE"] = "false"
    os.environ["CLICKHOUSE_PORT"] = "9000"

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "http"
    assert client_config["secure"] is False
    assert client_config["port"] == 9000


def test_interface_https_with_custom_port():
    """Test that interface is 'https' with custom port when CLICKHOUSE_SECURE=true."""
    os.environ["CLICKHOUSE_HOST"] = "example.com"
    os.environ["CLICKHOUSE_USER"] = "test"
    os.environ["CLICKHOUSE_PASSWORD"] = "test"
    os.environ["CLICKHOUSE_SECURE"] = "true"
    os.environ["CLICKHOUSE_PORT"] = "9443"

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "https"
    assert client_config["secure"] is True
    assert client_config["port"] == 9443
