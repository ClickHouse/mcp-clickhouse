import asyncio
import sys
import types
import warnings
from pathlib import Path

import fastmcp
import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.utilities.mcp_server_config import MCPServerConfig as FastMCPFileConfig
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
        "CLICKHOUSE_MCP_AUTH_MODULE",
        "CLICKHOUSE_MCP_AUTH_TOKEN",
        "CLICKHOUSE_MCP_BIND_HOST",
        "CLICKHOUSE_MCP_BIND_PORT",
        "FASTMCP_SERVER_AUTH",
    ):
        monkeypatch.delenv(name, raising=False)


def _install_auth_module(monkeypatch: pytest.MonkeyPatch, name: str, token: str) -> None:
    """Register an in-memory CLICKHOUSE_MCP_AUTH_MODULE returning a static verifier."""
    module = types.ModuleType(name)
    module.create_auth_provider = lambda: StaticTokenVerifier(
        tokens={token: {"client_id": "module-client", "scopes": []}},
        required_scopes=[],
    )
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_MODULE", name)


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


def test_http_app_restores_auth_between_static_and_module_construction(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_http_env(monkeypatch)
    constructor_provider = StaticTokenVerifier(
        tokens={"constructor-token": {"client_id": "constructor-client", "scopes": []}},
        required_scopes=[],
    )
    server = ClickHouseFastMCP("test", auth=constructor_provider)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "static-token")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")

    server.http_app(transport="http")

    assert server.auth is constructor_provider
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_TOKEN")
    _install_auth_module(monkeypatch, "boundary_auth_module", token="module-token")
    module_app = server.http_app(transport="http")
    assert server.auth is constructor_provider

    with TestClient(module_app) as client:
        response = client.post(
            "/mcp",
            json=_INITIALIZE_REQUEST,
            headers={**_MCP_HEADERS, "authorization": "Bearer module-token"},
        )

    assert response.status_code == 200


def test_module_provider_gates_the_mcp_endpoint(monkeypatch: pytest.MonkeyPatch):
    """A CLICKHOUSE_MCP_AUTH_MODULE provider rejects missing and foreign tokens."""
    _clear_http_env(monkeypatch)
    server = ClickHouseFastMCP("test")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "static-token")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")

    server.http_app(transport="http")

    assert server.auth is None
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_TOKEN")
    _install_auth_module(monkeypatch, "boundary_gate_module", token="module-token")
    app = server.http_app(transport="http")
    assert server.auth is None

    with TestClient(app) as client:
        missing = client.post("/mcp", json=_INITIALIZE_REQUEST, headers=_MCP_HEADERS)
        stale_static = client.post(
            "/mcp",
            json=_INITIALIZE_REQUEST,
            headers={**_MCP_HEADERS, "authorization": "Bearer static-token"},
        )
        accepted = client.post(
            "/mcp",
            json=_INITIALIZE_REQUEST,
            headers={**_MCP_HEADERS, "authorization": "Bearer module-token"},
        )

    assert missing.status_code == 401
    assert stale_static.status_code == 401
    assert accepted.status_code == 200
    assert "module-token" not in missing.text
    assert "static-token" not in stale_static.text


def test_http_app_rejects_legacy_fastmcp_server_auth(monkeypatch: pytest.MonkeyPatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH", "example.OAuthProvider")

    with pytest.raises(ValueError, match="CLICKHOUSE_MCP_AUTH_MODULE"):
        ClickHouseFastMCP("test").http_app(transport="http")


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
    """FastMCP 4 reads the default transport path from its global settings."""
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setattr(fastmcp.settings, setting_name, "/health")
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
