import pytest

from mcp_clickhouse.mcp_env import MCPServerConfig


def test_auth_token_configuration(monkeypatch: pytest.MonkeyPatch):
    """Test that auth token is correctly configured when set."""
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "test-secret-token")

    config = MCPServerConfig()

    assert config.auth_token == "test-secret-token"
    assert config.auth_disabled is False


def test_auth_disabled_configuration(monkeypatch: pytest.MonkeyPatch):
    """Test that auth can be disabled when CLICKHOUSE_MCP_AUTH_DISABLED=true."""
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_TOKEN", raising=False)

    config = MCPServerConfig()

    assert config.auth_disabled is True
    assert config.auth_token is None


def test_auth_enabled_by_default(monkeypatch: pytest.MonkeyPatch):
    """Test that auth is enabled by default (auth_disabled=False)."""
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_TOKEN", raising=False)

    config = MCPServerConfig()

    assert config.auth_disabled is False
    assert config.auth_token is None


def test_auth_token_with_stdio_transport(monkeypatch: pytest.MonkeyPatch):
    """Test that auth token is available but not required for stdio transport."""
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "stdio")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "test-token")

    config = MCPServerConfig()

    assert config.server_transport == "stdio"
    assert config.auth_token == "test-token"


def test_auth_token_with_http_transport(monkeypatch: pytest.MonkeyPatch):
    """Test that auth token is correctly configured for HTTP transport."""
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "http-auth-token")

    config = MCPServerConfig()

    assert config.server_transport == "http"
    assert config.auth_token == "http-auth-token"
    assert config.auth_disabled is False


def test_auth_token_with_sse_transport(monkeypatch: pytest.MonkeyPatch):
    """Test that auth token is correctly configured for SSE transport."""
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "sse")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "sse-auth-token")

    config = MCPServerConfig()

    assert config.server_transport == "sse"
    assert config.auth_token == "sse-auth-token"
    assert config.auth_disabled is False
