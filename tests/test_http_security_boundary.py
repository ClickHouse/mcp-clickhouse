import asyncio
import warnings
from pathlib import Path

import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.utilities.mcp_server_config import MCPServerConfig as FastMCPFileConfig
from starlette.exceptions import StarletteDeprecationWarning
from starlette.responses import PlainTextResponse

with warnings.catch_warnings():
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient

import mcp_clickhouse.mcp_server as mcp_server_module
from mcp_clickhouse.mcp_server import ClickHouseFastMCP

_INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1"},
    },
}
_MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


def _clear_http_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CLICKHOUSE_MCP_ALLOWED_HOSTS",
        "CLICKHOUSE_MCP_ALLOWED_ORIGINS",
        "CLICKHOUSE_MCP_AUTH_DISABLED",
        "CLICKHOUSE_MCP_AUTH_TOKEN",
        "CLICKHOUSE_MCP_BIND_HOST",
        "CLICKHOUSE_MCP_BIND_PORT",
        "FASTMCP_SERVER_AUTH",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "transport,path",
    [("http", "/mcp"), ("sse", "/sse")],
)
def test_exported_app_rejects_hostile_origin_before_auth(
    monkeypatch: pytest.MonkeyPatch, transport: str, path: str
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret-token")
    server = ClickHouseFastMCP("test")
    app = server.http_app(transport=transport)

    response = TestClient(app).request(
        "POST" if transport == "http" else "GET",
        path,
        headers={"host": "localhost:8000", "origin": "http://attacker.example"},
    )

    assert response.status_code == 403
    assert response.text == "Invalid Origin header"


@pytest.mark.parametrize(
    "transport,path",
    [("http", "/mcp"), ("sse", "/sse")],
)
def test_exported_app_rejects_hostile_host_by_default(
    monkeypatch: pytest.MonkeyPatch, transport: str, path: str
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    server = ClickHouseFastMCP("test")
    app = server.http_app(transport=transport)

    response = TestClient(app).request(
        "POST" if transport == "http" else "GET",
        path,
        headers={"host": "attacker.example"},
    )

    assert response.status_code == 421
    assert response.text == "Invalid Host header"


def test_static_auth_is_enforced_on_sse(monkeypatch: pytest.MonkeyPatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    app = ClickHouseFastMCP("test").http_app(transport="sse")

    response = TestClient(app).get("/sse")

    assert response.status_code == 401


def test_http_app_allows_configured_request_with_auth_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", "http://client.example")
    app = ClickHouseFastMCP("test").http_app(transport="http")

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json=_INITIALIZE_REQUEST,
            headers={**_MCP_HEADERS, "origin": "http://client.example"},
        )

    assert response.status_code == 200


def test_sse_app_allows_configured_request_with_auth_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    app = ClickHouseFastMCP("test").http_app(transport="sse")

    with TestClient(app) as client:
        response = client.post("/messages/?session_id=missing", json={"jsonrpc": "2.0"})

    assert response.status_code == 400
    assert response.text == "Invalid session ID"


def test_static_auth_is_enforced_and_accepts_the_configured_token(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    app = ClickHouseFastMCP("test").http_app(transport="http")

    with TestClient(app) as client:
        unauthorized = client.post("/mcp", json=_INITIALIZE_REQUEST, headers=_MCP_HEADERS)
        authorized = client.post(
            "/mcp",
            json=_INITIALIZE_REQUEST,
            headers={**_MCP_HEADERS, "authorization": "Bearer secret-token"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_http_app_restores_auth_between_static_and_oauth_construction(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    oauth_provider = StaticTokenVerifier(
        tokens={"oauth-token": {"client_id": "oauth-client", "scopes": []}},
        required_scopes=[],
    )
    server = ClickHouseFastMCP("test", auth=oauth_provider)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "static-token")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")

    server.http_app(transport="http")

    assert server.auth is oauth_provider
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_TOKEN")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH", "example.OAuthProvider")
    oauth_app = server.http_app(transport="http")
    assert server.auth is oauth_provider

    with TestClient(oauth_app) as client:
        response = client.post(
            "/mcp",
            json=_INITIALIZE_REQUEST,
            headers={**_MCP_HEADERS, "authorization": "Bearer oauth-token"},
        )

    assert response.status_code == 200


def test_oauth_cannot_reuse_temporary_static_provider(monkeypatch: pytest.MonkeyPatch):
    _clear_http_env(monkeypatch)
    server = ClickHouseFastMCP("test")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "static-token")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")

    server.http_app(transport="http")

    assert server.auth is None
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_TOKEN")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH", "example.OAuthProvider")
    with pytest.raises(ValueError, match="did not create an authentication provider"):
        server.http_app(transport="http")


def test_http_app_detects_positional_transport(monkeypatch: pytest.MonkeyPatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    seen_transports = []
    resolve_auth = mcp_server_module._resolve_auth

    def recording_resolve_auth(config, transport=None):
        seen_transports.append(transport)
        return resolve_auth(config, transport=transport)

    monkeypatch.setattr(mcp_server_module, "_resolve_auth", recording_resolve_auth)

    ClickHouseFastMCP("test").http_app(None, None, None, None, "sse")

    assert seen_transports == ["sse"]


@pytest.mark.parametrize(
    "transport,positional",
    [
        pytest.param("http", False, id="http-keyword"),
        pytest.param("streamable-http", False, id="streamable-http-keyword"),
        pytest.param("sse", False, id="sse-keyword"),
        pytest.param("sse", True, id="sse-positional"),
    ],
)
def test_http_app_rejects_health_transport_path(
    monkeypatch: pytest.MonkeyPatch, transport: str, positional: bool
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    server = ClickHouseFastMCP("test")

    with pytest.raises(ValueError, match="MCP transport path cannot be /health"):
        if positional:
            server.http_app("/health", None, None, None, transport)
        else:
            server.http_app(path="/health", transport=transport)


@pytest.mark.parametrize(
    "transport, setting_name",
    [
        ("http", "streamable_http_path"),
        ("sse", "sse_path"),
    ],
)
def test_http_app_rejects_health_transport_path_from_fastmcp_settings(
    monkeypatch: pytest.MonkeyPatch, transport: str, setting_name: str
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        server = ClickHouseFastMCP("test", **{setting_name: "/health"})

    with pytest.raises(ValueError, match="MCP transport path cannot be /health"):
        server.http_app(transport=transport)


def test_health_route_is_unauthenticated_and_exempt_from_transport_validation(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", "http://client.example")
    server = ClickHouseFastMCP("test")

    @server.custom_route("/health", methods=["GET"])
    async def health(request):
        return PlainTextResponse("OK")

    app = server.http_app(transport="http")
    client = TestClient(app)

    health_response = client.get(
        "/health",
        headers={"host": "attacker.example", "origin": "http://attacker.example"},
    )
    head = client.head(
        "/health",
        headers={"host": "attacker.example", "origin": "http://attacker.example"},
    )
    nearby = client.get(
        "/health/",
        headers={"host": "attacker.example", "origin": "http://attacker.example"},
    )
    post = client.post(
        "/health",
        headers={"host": "attacker.example", "origin": "http://attacker.example"},
    )

    assert health_response.status_code == 200
    assert health_response.text == "OK"
    assert head.status_code == 200
    assert head.text == ""
    assert nearby.status_code == 403
    assert nearby.text == "Invalid Origin header"
    assert post.status_code == 403
    assert post.text == "Invalid Origin header"


def test_runtime_http_override_requires_auth(monkeypatch: pytest.MonkeyPatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "stdio")
    server = ClickHouseFastMCP("test")

    with pytest.raises(ValueError, match="Authentication is required"):
        asyncio.run(server.run_async(transport="http", show_banner=False))


@pytest.mark.asyncio
async def test_fastmcp_json_loads_the_protected_exported_server(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "stdio")
    config_path = Path(__file__).parents[1] / "fastmcp.json"
    config = FastMCPFileConfig.from_file(config_path)

    loaded = await config.source.load_server()

    assert isinstance(loaded, FastMCP)
    with pytest.raises(ValueError, match="Authentication is required"):
        loaded.http_app(transport="http")

    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    app = loaded.http_app(transport="http")
    response = TestClient(app).post(
        "/mcp",
        headers={"host": "localhost:8000", "origin": "http://attacker.example"},
    )

    assert response.status_code == 403
