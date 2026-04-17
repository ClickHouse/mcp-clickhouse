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


_FASTMCP_JWT_VERIFIER = "fastmcp.server.auth.providers.jwt.JWTVerifier"


def _clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "CLICKHOUSE_MCP_AUTH_TOKEN",
        "CLICKHOUSE_MCP_AUTH_DISABLED",
        "FASTMCP_SERVER_AUTH",
    ):
        monkeypatch.delenv(var, raising=False)


def test_resolve_auth_stdio_returns_no_kwargs(monkeypatch: pytest.MonkeyPatch):
    """stdio transport never requires auth and never touches FastMCP auth kwargs."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "stdio")

    assert _resolve_auth(MCPServerConfig()) == {}


def test_resolve_auth_http_static_token(monkeypatch: pytest.MonkeyPatch):
    """A static token produces a StaticTokenVerifier passed into FastMCP."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret")

    kwargs = _resolve_auth(MCPServerConfig())

    assert isinstance(kwargs.get("auth"), StaticTokenVerifier)


def test_resolve_auth_http_fastmcp_oauth(monkeypatch: pytest.MonkeyPatch):
    """FASTMCP_SERVER_AUTH alone satisfies auth; kwargs stay empty so FastMCP auto-loads."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH", _FASTMCP_JWT_VERIFIER)

    assert _resolve_auth(MCPServerConfig()) == {}


def test_resolve_auth_http_fastmcp_oauth_constructs_server(
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end: FASTMCP_SERVER_AUTH with a real class path must actually let
    FastMCP(...) construct without raising. This guards against our docs or
    examples prescribing an invalid short-name that _resolve_auth accepts but
    FastMCP rejects at construction time."""
    from fastmcp import FastMCP

    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH", _FASTMCP_JWT_VERIFIER)
    # JWTVerifier needs at least one verification key source; a public JWKS URI
    # keeps the class importable without network use during init.
    monkeypatch.setenv(
        "FASTMCP_SERVER_AUTH_JWT_VERIFIER_JWKS_URI",
        "https://example.invalid/.well-known/jwks.json",
    )

    kwargs = _resolve_auth(MCPServerConfig())
    # Must not raise:
    FastMCP(name="test", **kwargs)


def test_resolve_auth_static_token_takes_precedence_over_fastmcp(
    monkeypatch: pytest.MonkeyPatch,
):
    """If both are set, static token wins (explicit over implicit)."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH", _FASTMCP_JWT_VERIFIER)

    kwargs = _resolve_auth(MCPServerConfig())

    assert isinstance(kwargs.get("auth"), StaticTokenVerifier)


def test_resolve_auth_disabled_explicitly_passes_none(monkeypatch: pytest.MonkeyPatch):
    """AUTH_DISABLED=true must pass auth=None so FastMCP won't auto-load OAuth env vars."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")

    assert _resolve_auth(MCPServerConfig()) == {"auth": None}


def test_resolve_auth_http_without_any_mode_raises(monkeypatch: pytest.MonkeyPatch):
    """HTTP transport with no auth config must fail startup."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")

    with pytest.raises(ValueError, match="FASTMCP_SERVER_AUTH"):
        _resolve_auth(MCPServerConfig())


def test_resolve_auth_sse_without_any_mode_raises(monkeypatch: pytest.MonkeyPatch):
    """SSE behaves the same as HTTP."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "sse")

    with pytest.raises(ValueError):
        _resolve_auth(MCPServerConfig())
