"""Tests for Host and Origin validation on the HTTP and SSE transports."""

import asyncio

import pytest
from starlette.middleware import Middleware

from mcp_clickhouse.http_security import (
    DNSRebindingProtectionMiddleware,
    transport_security_middleware,
)
from mcp_clickhouse.mcp_env import MCPServerConfig


class _RecordingApp:
    """Inner ASGI app that records whether it was reached."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"reached"})


def _send_request(middleware, path="/mcp", headers=None, scope_type="http"):
    """Drive one request through the middleware, returning (status, body)."""
    raw_headers = [
        (name.lower().encode(), value.encode()) for name, value in (headers or {}).items()
    ]
    scope = {
        "type": scope_type,
        "method": "POST",
        "path": path,
        "headers": raw_headers,
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(middleware(scope, receive, send))

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


def _middleware(app, allowed_hosts=("localhost:8000",), allowed_origins=()):
    return DNSRebindingProtectionMiddleware(
        app, allowed_hosts=allowed_hosts, allowed_origins=allowed_origins
    )


@pytest.mark.parametrize(
    "allowed_hosts, host, expected_status",
    [
        (["localhost:8000"], "localhost:8000", 200),
        (["localhost:8000", "127.0.0.1:8000"], "127.0.0.1:8000", 200),
        # The rebound request still carries the attacker's own hostname.
        (["localhost:8000"], "attacker.example.com:8000", 421),
        (["localhost:8000"], "localhost:9000", 421),
        # A host name is not a prefix match for a longer one.
        (["localhost:8000"], "localhost:80001", 421),
        (["evil.com"], "notevil.com", 421),
        # Any-port form.
        (["localhost:*"], "localhost:8000", 200),
        (["localhost:*"], "localhost:31337", 200),
        (["localhost:*"], "localhost", 421),
        (["localhost:*"], "attacker.example.com:8000", 421),
    ],
)
def test_host_header_is_checked_against_the_allow_list(allowed_hosts, host, expected_status):
    app = _RecordingApp()
    status, _ = _send_request(_middleware(app, allowed_hosts=allowed_hosts), headers={"host": host})

    assert status == expected_status
    assert app.called is (expected_status == 200)


def test_missing_host_header_is_rejected():
    """Every HTTP/1.1 client sends a Host header, so its absence is not served."""
    app = _RecordingApp()

    status, body = _send_request(_middleware(app), headers={})

    assert status == 421
    assert body == b"Invalid Host header"
    assert app.called is False


def test_empty_allow_list_rejects_every_host():
    app = _RecordingApp()

    status, _ = _send_request(
        _middleware(app, allowed_hosts=[]), headers={"host": "localhost:8000"}
    )

    assert status == 421
    assert app.called is False


@pytest.mark.parametrize(
    "allowed_origins, origin, expected_status",
    [
        # A request without an Origin header comes from a non-browser client.
        ((), None, 200),
        ((), "http://attacker.example.com", 403),
        (("http://localhost:3000",), "http://localhost:3000", 200),
        (("http://localhost:3000",), "http://attacker.example.com", 403),
        (("http://localhost:*",), "http://localhost:5173", 200),
    ],
)
def test_origin_header_is_checked_against_the_allow_list(allowed_origins, origin, expected_status):
    app = _RecordingApp()
    headers = {"host": "localhost:8000"}
    if origin is not None:
        headers["origin"] = origin

    status, _ = _send_request(_middleware(app, allowed_origins=allowed_origins), headers=headers)

    assert status == expected_status
    assert app.called is (expected_status == 200)


def test_host_is_checked_before_origin():
    """A bad host is reported as such even when the origin is also disallowed."""
    app = _RecordingApp()

    status, body = _send_request(
        _middleware(app, allowed_origins=["http://localhost:3000"]),
        headers={"host": "attacker.example.com", "origin": "http://attacker.example.com"},
    )

    assert status == 421
    assert body == b"Invalid Host header"


def test_health_endpoint_is_exempt():
    """Liveness probes arrive under a host name the operator cannot predict."""
    app = _RecordingApp()

    status, _ = _send_request(_middleware(app), path="/health", headers={"host": "10.1.2.3:8000"})

    assert status == 200
    assert app.called is True


def test_non_http_scopes_pass_through():
    """Lifespan and websocket traffic carries no headers to validate."""
    app = _RecordingApp()
    middleware = _middleware(app)
    scope = {"type": "lifespan"}

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        pass

    asyncio.run(middleware(scope, receive, send))

    assert app.called is True


class TestConfigParsing:
    """CLICKHOUSE_MCP_ALLOWED_HOSTS / _ORIGINS parsing."""

    def test_unset_returns_empty_lists(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", raising=False)

        config = MCPServerConfig()

        assert config.allowed_hosts == []
        assert config.allowed_origins == []

    def test_comma_separated_values_are_split_and_trimmed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", " localhost:8000 , 127.0.0.1:8000 ,, ")
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", "http://localhost:3000")

        config = MCPServerConfig()

        assert config.allowed_hosts == ["localhost:8000", "127.0.0.1:8000"]
        assert config.allowed_origins == ["http://localhost:3000"]


class TestMiddlewareFactory:
    """transport_security_middleware wiring."""

    def test_no_middleware_when_hosts_are_unset(self, monkeypatch: pytest.MonkeyPatch):
        """The server keeps its previous behavior until an operator opts in."""
        monkeypatch.delenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", raising=False)

        assert transport_security_middleware(MCPServerConfig()) == []

    def test_middleware_is_built_from_configured_hosts(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "localhost:8000")
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", "http://localhost:3000")

        built = transport_security_middleware(MCPServerConfig())

        assert len(built) == 1
        assert isinstance(built[0], Middleware)
        assert built[0].cls is DNSRebindingProtectionMiddleware
        assert built[0].kwargs["allowed_hosts"] == ["localhost:8000"]
        assert built[0].kwargs["allowed_origins"] == ["http://localhost:3000"]

    def test_origins_without_hosts_is_a_configuration_error(self, monkeypatch: pytest.MonkeyPatch):
        """Otherwise the configured origins would silently never be consulted."""
        monkeypatch.delenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", "http://localhost:3000")

        with pytest.raises(ValueError, match="CLICKHOUSE_MCP_ALLOWED_HOSTS"):
            transport_security_middleware(MCPServerConfig())
