import pytest

from mcp_server.config import MyScaleConfig


def test_interface_http_when_secure_false(monkeypatch: pytest.MonkeyPatch):
    """Test that interface is set to 'http' when CLICKHOUSE_SECURE=false."""
    monkeypatch.setenv("MYSCALE_HOST", "localhost")
    monkeypatch.setenv("MYSCALE_USER", "test")
    monkeypatch.setenv("MYSCALE_PASSWORD", "test")
    monkeypatch.setenv("MYSCALE_SECURE", "false")
    monkeypatch.setenv("MYSCALE_PORT", "8123")

    config = MyScaleConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "http"
    assert client_config["secure"] is False
    assert client_config["port"] == 8123


def test_interface_https_when_secure_true(monkeypatch: pytest.MonkeyPatch):
    """Test that interface is set to 'https' when MYSCALE_SECURE=true."""
    monkeypatch.setenv("MYSCALE_HOST", "example.com")
    monkeypatch.setenv("MYSCALE_USER", "test")
    monkeypatch.setenv("MYSCALE_PASSWORD", "test")
    monkeypatch.setenv("MYSCALE_SECURE", "true")
    monkeypatch.setenv("MYSCALE_PORT", "8443")

    config = MyScaleConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "https"
    assert client_config["secure"] is True
    assert client_config["port"] == 8443


def test_interface_https_by_default(monkeypatch: pytest.MonkeyPatch):
    """Test that interface defaults to 'https' when MYSCALE_SECURE is not set."""
    monkeypatch.setenv("MYSCALE_HOST", "example.com")
    monkeypatch.setenv("MYSCALE_USER", "test")
    monkeypatch.setenv("MYSCALE_PASSWORD", "test")
    monkeypatch.delenv("MYSCALE_SECURE", raising=False)
    monkeypatch.delenv("MYSCALE_PORT", raising=False)

    config = MyScaleConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "https"
    assert client_config["secure"] is True
    assert client_config["port"] == 8443


def test_interface_http_with_custom_port(monkeypatch: pytest.MonkeyPatch):
    """Test that interface is 'http' with custom port when MYSCALE_SECURE=false."""
    monkeypatch.setenv("MYSCALE_HOST", "localhost")
    monkeypatch.setenv("MYSCALE_USER", "test")
    monkeypatch.setenv("MYSCALE_PASSWORD", "test")
    monkeypatch.setenv("MYSCALE_SECURE", "false")
    monkeypatch.setenv("MYSCALE_PORT", "9000")

    config = MyScaleConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "http"
    assert client_config["secure"] is False
    assert client_config["port"] == 9000


def test_interface_https_with_custom_port(monkeypatch: pytest.MonkeyPatch):
    """Test that interface is 'https' with custom port when MYSCALE_SECURE=true."""
    monkeypatch.setenv("MYSCALE_HOST", "example.com")
    monkeypatch.setenv("MYSCALE_USER", "test")
    monkeypatch.setenv("MYSCALE_PASSWORD", "test")
    monkeypatch.setenv("MYSCALE_SECURE", "true")
    monkeypatch.setenv("MYSCALE_PORT", "9443")

    config = MyScaleConfig()
    client_config = config.get_client_config()

    assert client_config["interface"] == "https"
    assert client_config["secure"] is True
    assert client_config["port"] == 9443
