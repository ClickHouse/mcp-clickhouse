import asyncio
import warnings
from pathlib import Path

import fastmcp
import pytest
from fastmcp import FastMCP, settings as fastmcp_settings
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.settings import Settings as FastMCPSettings
from fastmcp.utilities.mcp_server_config import MCPServerConfig as FastMCPFileConfig
from starlette.applications import Starlette
from starlette.exceptions import StarletteDeprecationWarning
from starlette.responses import JSONResponse, PlainTextResponse

with warnings.catch_warnings():
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import mcp_clickhouse.mcp_server as mcp_server_module
import mcp_clickhouse.main as main_module
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
        "CLICKHOUSE_MCP_TRUSTED_PROXIES",
        "CLICKHOUSE_MCP_AUTH_DISABLED",
        "CLICKHOUSE_MCP_AUTH_TOKEN",
        "CLICKHOUSE_MCP_BIND_HOST",
        "CLICKHOUSE_MCP_BIND_PORT",
        "FASTMCP_SERVER_AUTH",
        "FASTMCP_HTTP_HOST_ORIGIN_PROTECTION",
        "FASTMCP_HTTP_ALLOWED_HOSTS",
        "FASTMCP_HTTP_ALLOWED_ORIGINS",
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


def test_rebuilt_sse_app_enforces_static_auth_and_host(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    app = ClickHouseFastMCP("test").sse_app()

    with TestClient(app) as client:
        unauthorized = client.post(
            "/messages/?session_id=missing",
            json={"jsonrpc": "2.0"},
        )
        authorized = client.post(
            "/messages/?session_id=missing",
            headers={"authorization": "Bearer secret-token"},
            json={"jsonrpc": "2.0"},
        )
        invalid_host = client.post(
            "/messages/?session_id=missing",
            headers={
                "authorization": "Bearer secret-token",
                "host": "attacker.example",
            },
            json={"jsonrpc": "2.0"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 400
    assert authorized.text == "Invalid session ID"
    assert invalid_host.status_code == 421
    assert invalid_host.text == "Invalid Host header"


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


@pytest.mark.parametrize(
    "transport,path,method",
    [
        ("http", "/mcp", "POST"),
        ("streamable-http", "/mcp", "POST"),
        ("sse", "/messages/?session_id=missing", "POST"),
    ],
)
def test_trusted_proxy_host_is_applied_at_fastmcp_boundary(
    monkeypatch: pytest.MonkeyPatch, transport: str, path: str, method: str
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")
    app = ClickHouseFastMCP("test").http_app(
        transport=transport,
        raw_client_address_preserved=True,
    )
    request_kwargs = {"json": _INITIALIZE_REQUEST, "headers": _MCP_HEADERS}
    if transport == "sse":
        request_kwargs = {"json": {"jsonrpc": "2.0"}}

    with TestClient(app, client=("10.0.0.8", 1234)) as client:
        response = client.request(
            method,
            path,
            headers={
                **request_kwargs.pop("headers", {}),
                "host": "internal-upstream",
                "x-forwarded-host": "mcp.example.com",
            },
            **request_kwargs,
        )

    assert response.status_code == (400 if transport == "sse" else 200)


def test_host_validation_runs_before_proxy_headers_are_applied(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")
    server = ClickHouseFastMCP("test")

    @server.custom_route("/scope", methods=["GET"])
    async def scope_details(request):
        return JSONResponse(
            {"client": request.client.host, "scheme": request.url.scheme}
        )

    app = server.http_app(transport="http", raw_client_address_preserved=True)
    with TestClient(app, client=("10.0.0.8", 1234)) as client:
        response = client.get(
            "/scope",
            headers={
                "host": "internal-upstream",
                "x-forwarded-host": "mcp.example.com",
                "x-forwarded-for": "198.51.100.20",
                "x-forwarded-proto": "https",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"client": "198.51.100.20", "scheme": "https"}


def test_ipv4_mapped_peer_passes_host_validation_and_proxy_headers(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")
    server = ClickHouseFastMCP("test")

    @server.custom_route("/scope", methods=["GET"])
    async def scope_details(request):
        return JSONResponse(
            {"client": request.client.host, "scheme": request.url.scheme}
        )

    app = server.http_app(transport="http", raw_client_address_preserved=True)
    with TestClient(app, client=("::ffff:10.0.0.8", 1234)) as client:
        response = client.get(
            "/scope",
            headers={
                "host": "internal-upstream",
                "x-forwarded-host": "mcp.example.com",
                "x-forwarded-for": "198.51.100.20",
                "x-forwarded-proto": "https",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"client": "198.51.100.20", "scheme": "https"}


def _proxy_headers_entries(app):
    return [entry for entry in app.user_middleware if entry.cls is ProxyHeadersMiddleware]


def test_proxy_headers_trust_includes_ipv4_mapped_forms(monkeypatch: pytest.MonkeyPatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.20.0.8,10.0.0.0/24,2001:db8::1")

    app = ClickHouseFastMCP("test").http_app(
        transport="http", raw_client_address_preserved=True
    )

    entries = _proxy_headers_entries(app)
    assert len(entries) == 1
    assert entries[0].kwargs["trusted_hosts"] == [
        "10.20.0.8/32",
        "::ffff:10.20.0.8/128",
        "10.0.0.0/24",
        "::ffff:10.0.0.0/120",
        "2001:db8::1/128",
    ]


def test_raw_client_assertion_without_trusted_proxies_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")

    app = ClickHouseFastMCP("test").http_app(
        transport="http", raw_client_address_preserved=True
    )

    assert _proxy_headers_entries(app) == []
    with TestClient(app) as client:
        response = client.post("/mcp", json=_INITIALIZE_REQUEST, headers=_MCP_HEADERS)
    assert response.status_code == 200


@pytest.mark.parametrize(
    "app_method,path,method",
    [
        ("sse_app", "/sse", "GET"),
        ("streamable_http_app", "/mcp", "POST"),
    ],
)
def test_legacy_app_builders_carry_transport_security(
    monkeypatch: pytest.MonkeyPatch, app_method: str, path: str, method: str
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    app = getattr(ClickHouseFastMCP("test"), app_method)()

    response = TestClient(app).request(method, path, headers={"host": "attacker.example"})

    assert response.status_code == 421
    assert response.text == "Invalid Host header"


@pytest.mark.parametrize("app_method", ["sse_app", "streamable_http_app"])
def test_legacy_app_builders_respect_the_trusted_proxy_embedding_guard(
    monkeypatch: pytest.MonkeyPatch, app_method: str
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")
    server = ClickHouseFastMCP("test")

    with pytest.raises(ValueError, match="raw ASGI client address"):
        getattr(server, app_method)()

    app = getattr(server, app_method)(raw_client_address_preserved=True)
    assert len(_proxy_headers_entries(app)) == 1


def test_sse_app_uses_custom_message_route_without_mutating_settings(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    server = ClickHouseFastMCP("test")
    original_message_path = fastmcp_settings.message_path

    app = server.sse_app(message_path="/custom-messages/")

    assert fastmcp_settings.message_path == original_message_path
    assert any(getattr(route, "path", None) == "/custom-messages" for route in app.routes)


def test_sse_app_custom_message_route_does_not_change_later_default_app(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    server = ClickHouseFastMCP("test")
    original_message_path = fastmcp_settings.message_path

    custom_app = server.sse_app(message_path="/custom-messages/")
    default_app = server.sse_app()

    assert fastmcp_settings.message_path == original_message_path
    assert any(getattr(route, "path", None) == "/custom-messages" for route in custom_app.routes)
    assert not any(
        getattr(route, "path", None) == "/custom-messages" for route in default_app.routes
    )
    assert any(
        getattr(route, "path", None) == original_message_path.rstrip("/")
        for route in default_app.routes
    )


@pytest.mark.parametrize("protection", ["true", "auto"])
def test_repo_host_guard_overrides_fastmcp_host_origin_settings(
    monkeypatch: pytest.MonkeyPatch,
    protection,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "repo.example")
    monkeypatch.setenv("FASTMCP_HTTP_HOST_ORIGIN_PROTECTION", protection)
    monkeypatch.setenv("FASTMCP_HTTP_ALLOWED_HOSTS", '["fastmcp.example"]')
    monkeypatch.setenv("FASTMCP_HTTP_ALLOWED_ORIGINS", '["https://fastmcp.example"]')
    configured_settings = FastMCPSettings()
    monkeypatch.setattr(fastmcp, "settings", configured_settings)
    monkeypatch.setattr(mcp_server_module, "fastmcp_settings", configured_settings)
    app = ClickHouseFastMCP("test").http_app(transport="http")

    with TestClient(app, base_url="http://repo.example") as client:
        allowed = client.post("/mcp", json=_INITIALIZE_REQUEST, headers=_MCP_HEADERS)
        rejected = client.post(
            "/mcp",
            json=_INITIALIZE_REQUEST,
            headers={**_MCP_HEADERS, "host": "fastmcp.example"},
        )

    assert configured_settings.http_host_origin_protection in {True, "auto"}
    assert allowed.status_code == 200
    assert rejected.status_code == 421
    assert rejected.text == "Invalid Host header"


def test_sse_app_keeps_registered_health_route(monkeypatch: pytest.MonkeyPatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")

    app = mcp_server_module.mcp.sse_app()

    assert any(getattr(route, "path", None) == "/health" for route in app.routes)


def test_sse_app_rejects_health_message_path(monkeypatch: pytest.MonkeyPatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")

    with pytest.raises(ValueError, match="MCP transport path cannot be /health"):
        ClickHouseFastMCP("test").sse_app(message_path="/health")


def test_direct_http_app_requires_raw_client_address_assertion(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")

    with pytest.raises(ValueError, match="raw ASGI client address"):
        ClickHouseFastMCP("test").http_app(transport="http")


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
    monkeypatch.setattr(
        mcp_server_module,
        "_load_fastmcp_auth_provider",
        lambda _provider_path, **_kwargs: oauth_provider,
    )
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
    with pytest.raises(ValueError, match="Could not import FASTMCP_SERVER_AUTH provider"):
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
    monkeypatch.setattr(fastmcp_settings, setting_name, "/health")
    server = ClickHouseFastMCP("test")

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


def test_run_http_async_disables_outer_proxy_header_processing(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")
    called = {}

    async def fake_run_http_async(self, uvicorn_config=None, transport="http", **kwargs):
        called.update(kwargs)
        called["transport"] = transport
        called["uvicorn_config"] = uvicorn_config
        called["app"] = self.http_app(transport=transport)

    monkeypatch.setattr(FastMCP, "run_http_async", fake_run_http_async)

    asyncio.run(
        ClickHouseFastMCP("test").run_http_async(
            show_banner=False,
            uvicorn_config={"limit_concurrency": 10},
        )
    )

    assert called["uvicorn_config"] == {"limit_concurrency": 10, "proxy_headers": False}
    assert called["app"] is not None


def test_run_http_async_rejects_conflicting_proxy_header_processing(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")

    with pytest.raises(ValueError, match="proxy_headers.*must be false"):
        asyncio.run(
            ClickHouseFastMCP("test").run_http_async(
                show_banner=False,
                uvicorn_config={"proxy_headers": True},
            )
        )


def test_run_http_async_preserves_uvicorn_behavior_without_trusted_proxies(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    uvicorn_config = {"proxy_headers": True}
    called = {}

    async def fake_run_http_async(self, uvicorn_config=None, transport="http", **kwargs):
        called.update(kwargs)
        called["transport"] = transport
        called["uvicorn_config"] = uvicorn_config

    monkeypatch.setattr(FastMCP, "run_http_async", fake_run_http_async)

    asyncio.run(
        ClickHouseFastMCP("test").run_http_async(
            show_banner=False,
            uvicorn_config=uvicorn_config,
        )
    )

    assert called["uvicorn_config"] is uvicorn_config


def test_http_app_adapts_to_older_fastmcp_signature(monkeypatch: pytest.MonkeyPatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    seen = {}

    def old_http_app(
        self,
        path=None,
        middleware=None,
        json_response=None,
        stateless_http=None,
        transport="http",
    ):
        seen["transport"] = transport
        app = Starlette()
        app.state.path = path or ("/sse" if transport == "sse" else "/mcp")
        return app

    monkeypatch.setattr(FastMCP, "http_app", old_http_app)

    ClickHouseFastMCP("test").http_app(None, None, None, None, "sse")

    assert seen["transport"] == "sse"


def test_trusted_proxy_runner_requires_upstream_uvicorn_config_support(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")
    called = False

    async def old_run_http_async(
        self,
        show_banner=True,
        transport="http",
        host=None,
        port=None,
        log_level=None,
        path=None,
    ):
        nonlocal called
        called = True

    monkeypatch.setattr(FastMCP, "run_http_async", old_run_http_async)

    with pytest.raises(RuntimeError, match="supports uvicorn_config"):
        asyncio.run(ClickHouseFastMCP("test").run_http_async(show_banner=False))

    assert called is False


def _run_http_async_without_middleware_option(called):
    """Upstream runner stand-in whose signature lacks the middleware option."""

    async def old_run_http_async(
        self,
        show_banner=True,
        transport="http",
        host=None,
        port=None,
        log_level=None,
        path=None,
        uvicorn_config=None,
    ):
        called["uvicorn_config"] = uvicorn_config
        called["app"] = self.http_app(transport=transport)

    return old_run_http_async


def test_trusted_proxy_runner_adapts_to_fewer_upstream_options(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")
    called = {}

    monkeypatch.setattr(
        FastMCP, "run_http_async", _run_http_async_without_middleware_option(called)
    )

    asyncio.run(ClickHouseFastMCP("test").run_http_async(show_banner=False))

    assert called["uvicorn_config"] == {"proxy_headers": False}
    assert called["app"] is not None


def test_trusted_proxy_runner_rejects_options_the_upstream_lacks(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")

    monkeypatch.setattr(
        FastMCP, "run_http_async", _run_http_async_without_middleware_option({})
    )

    with pytest.raises(TypeError, match="middleware"):
        asyncio.run(
            ClickHouseFastMCP("test").run_http_async(
                show_banner=False,
                middleware=[object()],
            )
        )


def test_builtin_runner_raw_client_assertion_is_consumed_by_one_app(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")
    results = {}

    async def fake_run_http_async(self, uvicorn_config=None, transport="http", **kwargs):
        results["app"] = self.http_app(transport=transport)
        with pytest.raises(ValueError, match="raw ASGI client address"):
            self.http_app(transport=transport)

    monkeypatch.setattr(FastMCP, "run_http_async", fake_run_http_async)

    asyncio.run(ClickHouseFastMCP("test").run_http_async(show_banner=False))

    assert results["app"] is not None


def test_project_cli_uses_the_secured_builtin_http_runner(monkeypatch: pytest.MonkeyPatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_MCP_BIND_PORT", "4200")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")
    called = {}

    async def fake_run_http_async(self, uvicorn_config=None, transport="http", **kwargs):
        called.update(kwargs)
        called["transport"] = transport
        called["uvicorn_config"] = uvicorn_config

    monkeypatch.setattr(FastMCP, "run_http_async", fake_run_http_async)
    monkeypatch.setattr(main_module, "setup_middleware", lambda server: None)

    main_module.main()

    assert called["transport"] == "http"
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 4200
    assert called["uvicorn_config"] == {"proxy_headers": False}


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


@pytest.mark.asyncio
async def test_fastmcp_json_requires_raw_peer_assertion_for_direct_embedding(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "stdio")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")
    config_path = Path(__file__).parents[1] / "fastmcp.json"
    loaded = await FastMCPFileConfig.from_file(config_path).source.load_server()

    with pytest.raises(ValueError, match="raw ASGI client address"):
        loaded.http_app(transport="http")
