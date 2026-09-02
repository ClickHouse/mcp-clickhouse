"""HTTP transport contract tests against the real exported ASGI app.

These tests build the app through ``mcp_clickhouse.mcp_server.mcp.http_app(...)``
with a real ``TestClient``, matching the pattern in test_health_endpoint.py and
test_http_security_boundary.py. They pin three upstream-facing behaviors that
are easy to break silently on a FastMCP bump:

1. Protocol version negotiation over a plain ``initialize`` POST (the
   handshake-era wire) and the sessionless 2026-07-28 wire, which the MCP SDK
   selects from the ``MCP-Protocol-Version`` request header.
2. ``stateless_http`` behavior for the hostile-Host, unauthenticated, and
   session-id-issuance cases.
3. The upstream default for ``fastmcp.settings.http_host_origin_protection``,
   which this project's own DNSRebindingProtectionMiddleware substitutes for
   (MIGRATION_DECISIONS.md D7).
"""

import warnings

import fastmcp
import pytest
from fastmcp.server.http import HostOriginGuardMiddleware
from mcp.shared.inbound import MCP_METHOD_HEADER, MCP_PROTOCOL_VERSION_HEADER
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)
from starlette.exceptions import StarletteDeprecationWarning

with warnings.catch_warnings():
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient

from mcp_clickhouse import mcp_server
from tests.helpers import MCP_HEADERS, initialize_request, jsonrpc_body


# Verified empirically against fastmcp 4.0.1 / mcp 2.1.1: the first three
# requested versions are echoed back unchanged. The SDK routes a request to the
# sessionless 2026-07-28 wire from the MCP-Protocol-Version header, not from the
# initialize body, so an initialize POST asking for 2026-07-28 without that
# header is served by the handshake-era transport and downgraded to 2025-11-25.
@pytest.mark.parametrize(
    "requested_version,expected_version",
    [
        ("2025-03-26", "2025-03-26"),
        ("2025-06-18", "2025-06-18"),
        ("2025-11-25", "2025-11-25"),
        ("2026-07-28", "2025-11-25"),
    ],
)
def test_protocol_version_negotiation(
    authenticated_app_env, requested_version: str, expected_version: str
):
    """initialize negotiates protocolVersion and always issues a session id.

    Every initialize handshake, including one that asks for the sessionless
    2026-07-28 version, ends in a handshake-era session with an mcp-session-id
    and therefore a session-scoped context-state store. That is why the
    serializable=False requirement in MIGRATION_DECISIONS.md D9 is load bearing
    for every client that performs a handshake. The sessionless wire, which
    issues no session at all, is covered by TestSessionlessWire below.
    """
    app = mcp_server.mcp.http_app(transport="http")

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json=initialize_request(requested_version),
            headers={**MCP_HEADERS, "authorization": "Bearer secret-token"},
        )

    assert response.status_code == 200
    body = jsonrpc_body(response)
    assert body["result"]["protocolVersion"] == expected_version
    # Every case above negotiates a stateful session (default stateless_http
    # is False), so a session id is always issued.
    assert "mcp-session-id" in response.headers


def _sessionless_tools_list_request() -> dict:
    """A 2026-07-28 request: no initialize, the envelope travels in params._meta."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                PROTOCOL_VERSION_META_KEY: "2026-07-28",
                CLIENT_CAPABILITIES_META_KEY: {},
                CLIENT_INFO_META_KEY: {"name": "test-client", "version": "1"},
            }
        },
    }


_SESSIONLESS_HEADERS = {
    **MCP_HEADERS,
    MCP_PROTOCOL_VERSION_HEADER: "2026-07-28",
    MCP_METHOD_HEADER: "tools/list",
}


class TestSessionlessWire:
    """The 2026-07-28 wire: selected by header, no session, different result envelope.

    Verified against mcp 2.1.1: the streamable HTTP manager dispatches to the
    modern entry when MCP-Protocol-Version names a version outside the
    handshake set. No mcp-session-id is issued, so the session-scoped
    context-state store is never involved, and the result carries the
    2026-07-28 envelope fields (resultType, cacheScope) alongside the payload.
    The auth and Host/Origin guards sit in front of the transport and apply
    unchanged.
    """

    def test_tools_list_is_served_without_a_session(self, authenticated_app_env):
        app = mcp_server.mcp.http_app(transport="http")

        with TestClient(app) as client:
            response = client.post(
                "/mcp",
                json=_sessionless_tools_list_request(),
                headers={**_SESSIONLESS_HEADERS, "authorization": "Bearer secret-token"},
            )

        assert response.status_code == 200
        assert "mcp-session-id" not in response.headers
        body = jsonrpc_body(response)
        assert "error" not in body, body
        assert body["result"]["resultType"] == "complete"
        assert {tool["name"] for tool in body["result"]["tools"]} >= {
            "list_databases",
            "list_tables",
            "run_query",
        }

    @pytest.mark.parametrize(
        "extra_headers,expected_status",
        [
            pytest.param({}, 401, id="no-token"),
            pytest.param(
                {"authorization": "Bearer secret-token", "host": "attacker.example"},
                421,
                id="hostile-host",
            ),
            pytest.param(
                {"authorization": "Bearer secret-token", "origin": "http://evil.example"},
                403,
                id="hostile-origin",
            ),
        ],
    )
    def test_gates_hold_on_the_sessionless_wire(
        self, authenticated_app_env, extra_headers: dict, expected_status: int
    ):
        app = mcp_server.mcp.http_app(transport="http")

        with TestClient(app) as client:
            response = client.post(
                "/mcp",
                json=_sessionless_tools_list_request(),
                headers={**_SESSIONLESS_HEADERS, **extra_headers},
            )

        assert response.status_code == expected_status
        assert "mcp-session-id" not in response.headers


@pytest.mark.parametrize("stateless", [True, False])
class TestStatelessHttpParametrized:
    """stateless_http controls whether a session id is issued, not the security gates."""

    def test_hostile_host_still_rejected(self, authenticated_app_env, stateless: bool):
        app = mcp_server.mcp.http_app(transport="http", stateless_http=stateless)

        with TestClient(app) as client:
            response = client.post(
                "/mcp",
                json=initialize_request("2025-11-25"),
                headers={
                    **MCP_HEADERS,
                    "host": "attacker.example",
                    "authorization": "Bearer secret-token",
                },
            )

        assert response.status_code == 421

    def test_unauthenticated_initialize_still_rejected(
        self, authenticated_app_env, stateless: bool
    ):
        app = mcp_server.mcp.http_app(transport="http", stateless_http=stateless)

        with TestClient(app) as client:
            response = client.post(
                "/mcp",
                json=initialize_request("2025-11-25"),
                headers=MCP_HEADERS,
            )

        assert response.status_code == 401

    def test_authenticated_initialize_session_id_tracks_stateless_flag(
        self, authenticated_app_env, stateless: bool
    ):
        """Verified empirically: FastMCP 4 only issues mcp-session-id when stateful.

        stateless_http=True builds a fresh transport per request with no
        session tracking, so no mcp-session-id header is issued.
        stateless_http=False (the project default) issues one, matching the
        protocol-negotiation test above.
        """
        app = mcp_server.mcp.http_app(transport="http", stateless_http=stateless)

        with TestClient(app) as client:
            init = client.post(
                "/mcp",
                json=initialize_request("2025-11-25"),
                headers={**MCP_HEADERS, "authorization": "Bearer secret-token"},
            )

        assert init.status_code == 200
        assert ("mcp-session-id" in init.headers) == (not stateless)


def test_upstream_host_origin_protection_default_is_off(authenticated_app_env):
    """This project's DNSRebindingProtectionMiddleware is the Host/Origin guard.

    MIGRATION_DECISIONS.md D7: FastMCP 4 ships its own host_origin_protection
    option, off by default, and this project deliberately leaves it off in
    favor of its own middleware (a /health exemption and
    X-Forwarded-Host/trusted-proxy support). If
    upstream ever flips this default to True or "auto", both layers would
    validate the same request with different status codes (this project
    returns 421/403, FastMCP's own guard uses different codes), so this test
    is the alarm that fires before that surprises anyone in production.
    """
    assert fastmcp.settings.http_host_origin_protection is False

    # Confirm the app this project builds does not also carry FastMCP's own
    # guard middleware, which would only happen if a caller passed
    # host_origin_protection explicitly to http_app (not part of this
    # project's public surface today).
    app = mcp_server.mcp.http_app(transport="http")
    assert not any(
        entry.cls is HostOriginGuardMiddleware for entry in app.user_middleware
    )
