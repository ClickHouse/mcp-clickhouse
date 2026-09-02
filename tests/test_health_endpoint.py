"""GET /health through the real exported ASGI app.

The direct handler tests in test_optional_chdb.py exercise ``health_check`` with a
hand-built Request, and test_http_security.py checks the exemption against a
recording stub app. These tests drive the module singleton's
``http_app`` so the real route, auth provider, and transport-security middleware are
all in the path.
"""

import warnings
from unittest.mock import patch

from starlette.exceptions import StarletteDeprecationWarning

with warnings.catch_warnings():
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient

from mcp_clickhouse import mcp_server
from tests.helpers import INITIALIZE_REQUEST, MCP_HEADERS

_HEALTH_FAILURE_BODY = "ERROR. ClickHouse connection failed. Check server logs for details."
_HOSTILE_HEADERS = {"host": "attacker.example", "origin": "http://attacker.example"}


def test_health_is_reachable_without_credentials_while_mcp_requires_them(
    authenticated_app_env,
):
    """Real ClickHouse probe, real route, real auth: /health is open, the MCP path is not."""
    app = mcp_server.mcp.http_app(transport="http")

    with (
        patch.object(
            mcp_server,
            "_probe_clickhouse_health",
            wraps=mcp_server._probe_clickhouse_health,
        ) as probe,
        TestClient(app) as client,
    ):
        health = client.get("/health")
        unauthenticated_mcp = client.post("/mcp", json=INITIALIZE_REQUEST, headers=MCP_HEADERS)

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


def test_health_exemption_from_host_and_origin_checks_is_exact_in_real_app(
    authenticated_app_env,
):
    app = mcp_server.mcp.http_app(transport="http")

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
