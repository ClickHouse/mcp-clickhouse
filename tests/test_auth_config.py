import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from mcp_clickhouse.mcp_env import MCPServerConfig
from mcp_clickhouse.mcp_server import _resolve_auth


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


def _clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "CLICKHOUSE_MCP_AUTH_TOKEN",
        "CLICKHOUSE_MCP_AUTH_DISABLED",
        "FASTMCP_SERVER_AUTH",
    ):
        monkeypatch.delenv(var, raising=False)


def test_resolve_auth_oauth_omits_auth_kwarg(monkeypatch: pytest.MonkeyPatch):
    """FASTMCP_SERVER_AUTH alone returns empty kwargs (no `auth` key)."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH", "fastmcp.server.auth.providers.jwt.JWTVerifier")

    assert _resolve_auth(MCPServerConfig()) == {}


def test_resolve_auth_disabled_passes_explicit_none(monkeypatch: pytest.MonkeyPatch):
    """CLICKHOUSE_MCP_AUTH_DISABLED=true returns {"auth": None}, not {}."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")

    assert _resolve_auth(MCPServerConfig()) == {"auth": None}


def test_resolve_auth_static_token_takes_precedence_over_oauth(
    monkeypatch: pytest.MonkeyPatch,
):
    """With both static token and FASTMCP_SERVER_AUTH set, returns the static verifier."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH", "fastmcp.server.auth.providers.jwt.JWTVerifier")

    assert isinstance(_resolve_auth(MCPServerConfig()).get("auth"), StaticTokenVerifier)


def test_resolve_auth_http_without_any_mode_raises(monkeypatch: pytest.MonkeyPatch):
    """HTTP transport with no auth configured raises ValueError."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")

    with pytest.raises(ValueError):
        _resolve_auth(MCPServerConfig())
