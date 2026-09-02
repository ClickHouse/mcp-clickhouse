"""GET /health through the real exported ASGI app.

The direct handler tests in test_optional_chdb.py exercise ``health_check`` with a
hand-built Request, and test_http_security.py checks the exemption against a
recording stub app. These tests drive the module singleton's
``http_app`` so the real route, auth provider, and transport-security middleware are
all in the path.
"""

import warnings
from unittest.mock import patch

import pytest
from starlette.exceptions import StarletteDeprecationWarning

with warnings.catch_warnings():
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient

from mcp_clickhouse import mcp_server

_HEALTH_FAILURE_BODY = "ERROR. ClickHouse connection failed. Check server logs for details."
_HOSTILE_HEADERS = {"host": "attacker.example", "origin": "http://attacker.example"}
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


@pytest.fixture
def authenticated_app_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "CLICKHOUSE_MCP_ALLOWED_HOSTS",
        "CLICKHOUSE_MCP_ALLOWED_ORIGINS",
        "CLICKHOUSE_MCP_TRUSTED_PROXIES",
        "CLICKHOUSE_MCP_AUTH_DISABLED",
        "CLICKHOUSE_MCP_AUTH_MODULE",
        "CLICKHOUSE_MCP_AUTH_TOKEN",
        "FASTMCP_SERVER_AUTH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLICKHOUSE_ENABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", "http://client.example")


@pytest.mark.parametrize(
    "transport,mcp_path,mcp_method",
    [("http", "/mcp", "POST"), ("sse", "/sse", "GET")],
)
def test_health_is_reachable_without_credentials_while_mcp_requires_them(
    authenticated_app_env, transport: str, mcp_path: str, mcp_method: str
):
    """Real ClickHouse probe, real route, real auth: /health is open, the MCP path is not."""
    app = mcp_server.mcp.http_app(transport=transport)

    with (
        patch.object(
            mcp_server,
            "_probe_clickhouse_health",
            wraps=mcp_server._probe_clickhouse_health,
        ) as probe,
        TestClient(app) as client,
    ):
        health = client.get("/health")
        mcp_kwargs = {"json": _INITIALIZE_REQUEST, "headers": _MCP_HEADERS}
        if transport == "sse":
            mcp_kwargs = {}
        unauthenticated_mcp = client.request(mcp_method, mcp_path, **mcp_kwargs)

    assert health.status_code == 200
    assert health.text == "OK"
    assert health.headers["content-type"].startswith("text/plain")
    assert probe.call_count == 1
    assert unauthenticated_mcp.status_code == 401


def test_health_failure_body_is_minimal_and_leaks_nothing(authenticated_app_env):
    def raise_with_secrets(_config):
        raise ConnectionError(
            "HTTPConnectionPool(host='internal-ch.prod.mycorp.local', port=8443): "
            "password=hunter2 failed for /etc/clickhouse/secrets"
        )

    app = mcp_server.mcp.http_app(transport="http")

    with (
        patch.object(
            mcp_server, "_probe_clickhouse_health", side_effect=raise_with_secrets
        ) as probe,
        TestClient(app) as client,
    ):
        response = client.get("/health")

    # The injected exception is the one the handler swallowed.
    probe.assert_called_once()
    assert response.status_code == 503
    assert response.text == _HEALTH_FAILURE_BODY
    lowered = response.text.lower()
    for secret in (
        "internal-ch.prod.mycorp.local",
        "hunter2",
        "/etc/clickhouse",
        "httpconnectionpool",
        "8443",
        "connectionerror",
    ):
        assert secret not in lowered


def test_health_body_never_carries_backend_version(authenticated_app_env):
    """A healthy probe returns exactly OK, so no ClickHouse version string can leak."""
    app = mcp_server.mcp.http_app(transport="http")

    with (
        patch.object(mcp_server, "_probe_clickhouse_health", return_value=None),
        TestClient(app) as client,
    ):
        response = client.get("/health")

    assert response.status_code == 200
    assert response.text == "OK"
    assert "26.8" not in response.text
    assert "clickhouse" not in response.text.lower()


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_health_exemption_from_host_and_origin_checks_is_exact_in_real_app(
    authenticated_app_env, transport: str
):
    app = mcp_server.mcp.http_app(transport=transport)

    with (
        patch.object(mcp_server, "_probe_clickhouse_health", return_value=None),
        TestClient(app) as client,
    ):
        get = client.get("/health", headers=_HOSTILE_HEADERS)
        head = client.head("/health", headers=_HOSTILE_HEADERS)
        trailing_slash = client.get("/health/", headers=_HOSTILE_HEADERS)
        post = client.post("/health", headers=_HOSTILE_HEADERS)
        hostile_host_only = client.get("/health", headers={"host": "attacker.example"})

    assert get.status_code == 200
    assert get.text == "OK"
    assert head.status_code == 200
    assert head.text == ""
    assert hostile_host_only.status_code == 200
    assert trailing_slash.status_code == 403
    assert trailing_slash.text == "Invalid Origin header"
    assert post.status_code == 403
    assert post.text == "Invalid Origin header"
