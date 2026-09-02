import os
import sys
import types

import pytest
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier, StaticTokenVerifier

from mcp_clickhouse.mcp_auth_hook import load_auth_provider
from mcp_clickhouse.mcp_env import MCPServerConfig
from mcp_clickhouse.mcp_server import _resolve_auth

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_AUTH_ENV_VARS = ("MCP_AUTH_JWKS_URI", "MCP_AUTH_ISSUER", "MCP_AUTH_AUDIENCE")


def _install_auth_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs) -> None:
    """Register an in-memory module so CLICKHOUSE_MCP_AUTH_MODULE can import it."""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


def _static_provider(token: str = "module-token") -> StaticTokenVerifier:
    return StaticTokenVerifier(
        tokens={token: {"client_id": "module-client", "scopes": []}},
        required_scopes=[],
    )


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
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_MODULE", raising=False)

    config = MCPServerConfig()

    assert config.auth_disabled is False
    assert config.auth_token is None
    assert config.auth_module is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("my_auth", "my_auth"),
        ("  pkg.auth_module  ", "pkg.auth_module"),
        ("", None),
        ("   ", None),
    ],
)
def test_auth_module_parsing(monkeypatch: pytest.MonkeyPatch, raw: str, expected):
    """CLICKHOUSE_MCP_AUTH_MODULE is trimmed and blank values are unset."""
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_MODULE", raw)

    assert MCPServerConfig().auth_module == expected


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
        "CLICKHOUSE_MCP_AUTH_MODULE",
        "FASTMCP_SERVER_AUTH",
    ):
        monkeypatch.delenv(var, raising=False)


def test_resolve_auth_stdio_returns_no_kwargs(monkeypatch: pytest.MonkeyPatch):
    """Non-HTTP transports do not resolve or require any auth mode."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "stdio")

    assert _resolve_auth(MCPServerConfig()) == {}


def test_resolve_auth_module_returns_the_provider(monkeypatch: pytest.MonkeyPatch):
    """CLICKHOUSE_MCP_AUTH_MODULE alone resolves to the module's provider."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    provider = _static_provider()
    _install_auth_module(monkeypatch, "auth_mod_ok", create_auth_provider=lambda: provider)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_MODULE", "auth_mod_ok")

    assert _resolve_auth(MCPServerConfig()) == {"auth": provider}


def test_resolve_auth_module_without_factory_raises(monkeypatch: pytest.MonkeyPatch):
    """A module without create_auth_provider() fails clearly."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    _install_auth_module(monkeypatch, "auth_mod_no_factory", create_auth_provider="not-callable")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_MODULE", "auth_mod_no_factory")

    with pytest.raises(ValueError, match="must define a callable create_auth_provider"):
        _resolve_auth(MCPServerConfig())


def test_resolve_auth_module_returning_wrong_type_raises(monkeypatch: pytest.MonkeyPatch):
    """create_auth_provider() must return a fastmcp AuthProvider."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    _install_auth_module(
        monkeypatch, "auth_mod_wrong_type", create_auth_provider=lambda: {"not": "a provider"}
    )
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_MODULE", "auth_mod_wrong_type")

    with pytest.raises(ValueError, match="expected a fastmcp.server.auth.AuthProvider"):
        _resolve_auth(MCPServerConfig())


def test_resolve_auth_module_import_failure_raises(monkeypatch: pytest.MonkeyPatch):
    """An unimportable module fails with an ImportError naming the module."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_MODULE", "definitely_missing_auth_module_xyz")

    with pytest.raises(ImportError, match="definitely_missing_auth_module_xyz"):
        _resolve_auth(MCPServerConfig())


def test_resolve_auth_module_factory_exception_is_wrapped(monkeypatch: pytest.MonkeyPatch):
    """An exception raised inside create_auth_provider() fails startup with context."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    original = RuntimeError("boom")

    def failing_factory():
        raise original

    _install_auth_module(monkeypatch, "auth_mod_raises", create_auth_provider=failing_factory)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_MODULE", "auth_mod_raises")

    with pytest.raises(ValueError) as exc_info:
        _resolve_auth(MCPServerConfig())

    message = str(exc_info.value)
    assert "CLICKHOUSE_MCP_AUTH_MODULE" in message
    assert "auth_mod_raises" in message
    assert "create_auth_provider" in message
    assert "RuntimeError: boom" in message
    assert exc_info.value.__cause__ is original


def test_resolve_auth_rejects_legacy_fastmcp_server_auth(monkeypatch: pytest.MonkeyPatch):
    """FASTMCP_SERVER_AUTH is rejected with a migration message, never ignored."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH", "fastmcp.server.auth.providers.jwt.JWTVerifier")

    with pytest.raises(ValueError, match="CLICKHOUSE_MCP_AUTH_MODULE"):
        _resolve_auth(MCPServerConfig())


def test_resolve_auth_rejects_legacy_fastmcp_server_auth_even_with_token(
    monkeypatch: pytest.MonkeyPatch,
):
    """A stale FASTMCP_SERVER_AUTH fails startup even when another mode is valid."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH", "fastmcp.server.auth.providers.jwt.JWTVerifier")

    with pytest.raises(ValueError, match="no longer supported"):
        _resolve_auth(MCPServerConfig())


def test_resolve_auth_disabled_passes_explicit_none(monkeypatch: pytest.MonkeyPatch):
    """CLICKHOUSE_MCP_AUTH_DISABLED=true returns {"auth": None}, not {}."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")

    assert _resolve_auth(MCPServerConfig()) == {"auth": None}


def test_resolve_auth_rejects_multiple_modes(monkeypatch: pytest.MonkeyPatch):
    """Configuring more than one auth mode raises ValueError."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret")
    _install_auth_module(monkeypatch, "auth_mod_unused", create_auth_provider=_static_provider)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_MODULE", "auth_mod_unused")

    with pytest.raises(ValueError, match="mutually exclusive") as exc_info:
        _resolve_auth(MCPServerConfig())

    assert "CLICKHOUSE_MCP_AUTH_MODULE" in str(exc_info.value)
    assert "secret" not in str(exc_info.value)


def test_resolve_auth_http_without_any_mode_raises(monkeypatch: pytest.MonkeyPatch):
    """HTTP transport with no auth configured raises ValueError naming every mode."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")

    with pytest.raises(ValueError) as exc_info:
        _resolve_auth(MCPServerConfig())

    message = str(exc_info.value)
    assert "CLICKHOUSE_MCP_AUTH_TOKEN" in message
    assert "CLICKHOUSE_MCP_AUTH_MODULE" in message
    assert "CLICKHOUSE_MCP_AUTH_DISABLED" in message


def test_load_auth_provider_accepts_any_auth_provider_subclass(monkeypatch: pytest.MonkeyPatch):
    """The hook validates against the AuthProvider base class, not a concrete type."""
    provider = _static_provider()
    _install_auth_module(monkeypatch, "auth_mod_direct", create_auth_provider=lambda: provider)

    loaded = load_auth_provider("auth_mod_direct")

    assert loaded is provider
    assert isinstance(loaded, AuthProvider)


@pytest.fixture
def example_auth_module(monkeypatch: pytest.MonkeyPatch):
    """Make the repo-root example_auth.py importable and scrub MCP_AUTH_* env vars.

    Every MCP_AUTH_* variable is cleared before the test runs. A pre-existing
    cached `example_auth` module is removed through monkeypatch so it is put
    back on teardown; the module imported during the test is popped directly,
    because monkeypatch.delitem on a key the test added would be undone and
    would leak the module into later tests.
    """
    for var in EXAMPLE_AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delitem(sys.modules, "example_auth", raising=False)
    monkeypatch.syspath_prepend(REPO_ROOT)
    yield
    sys.modules.pop("example_auth", None)


def test_load_auth_provider_example_auth_happy_path(
    monkeypatch: pytest.MonkeyPatch, example_auth_module
):
    """example_auth.create_auth_provider() builds a real fastmcp JWTVerifier."""
    monkeypatch.setenv("MCP_AUTH_JWKS_URI", "https://login.example.com/.well-known/jwks.json")
    monkeypatch.setenv("MCP_AUTH_ISSUER", "https://login.example.com/")
    monkeypatch.setenv("MCP_AUTH_AUDIENCE", "mcp-clickhouse")

    loaded = load_auth_provider("example_auth")

    assert isinstance(loaded, JWTVerifier)
    assert isinstance(loaded, AuthProvider)


@pytest.mark.parametrize("missing_var", EXAMPLE_AUTH_ENV_VARS)
def test_load_auth_provider_example_auth_missing_var_raises(
    monkeypatch: pytest.MonkeyPatch, example_auth_module, missing_var: str
):
    """Each MCP_AUTH_* variable required by example_auth.py is validated and named."""
    for var in EXAMPLE_AUTH_ENV_VARS:
        if var != missing_var:
            monkeypatch.setenv(var, "https://login.example.com/")

    with pytest.raises(ValueError) as exc_info:
        load_auth_provider("example_auth")

    message = str(exc_info.value)
    assert "example_auth" in message
    assert missing_var in message
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert missing_var in str(exc_info.value.__cause__)
