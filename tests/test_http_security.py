"""Tests for Host and Origin validation on the HTTP transport."""

import asyncio

import pytest
from starlette.middleware import Middleware

from mcp_clickhouse.http_security import (
    DNSRebindingProtectionMiddleware,
    transport_security_middleware,
)
from mcp_clickhouse.mcp_env import MCPServerConfig
from tests.helpers import RecordingApp, send_asgi_request


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
        (["localhost:*"], "localhost:8000.evil.com", 421),
        (["localhost:8000"], "LOCALHOST:8000", 200),
        (["localhost:*"], "attacker.example.com:8000", 421),
    ],
)
def test_host_header_is_checked_against_the_allow_list(allowed_hosts, host, expected_status):
    app = RecordingApp()
    status, _ = send_asgi_request(_middleware(app, allowed_hosts=allowed_hosts), headers={"host": host})

    assert status == expected_status
    assert app.called is (expected_status == 200)


def test_missing_host_header_is_rejected():
    """Every HTTP/1.1 client sends a Host header, so its absence is not served."""
    app = RecordingApp()

    status, body = send_asgi_request(_middleware(app), headers={})

    assert status == 421
    assert body == b"Invalid Host header"
    assert app.called is False


def test_empty_allow_list_rejects_every_host():
    app = RecordingApp()

    status, _ = send_asgi_request(
        _middleware(app, allowed_hosts=[]), headers={"host": "localhost:8000"}
    )

    assert status == 421
    assert app.called is False


@pytest.mark.parametrize(
    "allowed_origins, origin, expected_status",
    [
        # A request without an Origin header comes from a non-browser client.
        ((), None, 200),
        ((), "", 403),
        ((), "http://attacker.example.com", 403),
        (("http://localhost:3000",), "http://localhost:3000", 200),
        (("http://localhost:3000",), "http://attacker.example.com", 403),
        (("http://localhost:*",), "http://localhost:5173", 200),
    ],
)
def test_origin_header_is_checked_against_the_allow_list(allowed_origins, origin, expected_status):
    app = RecordingApp()
    headers = {"host": "localhost:8000"}
    if origin is not None:
        headers["origin"] = origin

    status, _ = send_asgi_request(_middleware(app, allowed_origins=allowed_origins), headers=headers)

    assert status == expected_status
    assert app.called is (expected_status == 200)


def test_invalid_origin_is_rejected_before_host_validation():
    """A present invalid Origin always receives the MCP-required 403."""
    app = RecordingApp()

    status, body = send_asgi_request(
        _middleware(app, allowed_origins=["http://localhost:3000"]),
        headers={"host": "attacker.example.com", "origin": "http://attacker.example.com"},
    )

    assert status == 403
    assert body == b"Invalid Origin header"


@pytest.mark.parametrize(
    "method, origin",
    [("GET", ""), ("GET", "http://attacker.example"), ("HEAD", "http://attacker.example")],
)
def test_health_endpoint_is_exempt_from_transport_validation(method, origin):
    app = RecordingApp()

    status, _ = send_asgi_request(
        _middleware(app),
        path="/health",
        headers={"host": "attacker.example", "origin": origin},
        method=method,
    )

    assert status == 200
    assert app.called is True


def test_health_exemption_uses_the_exact_path():
    app = RecordingApp()

    status, body = send_asgi_request(
        _middleware(app),
        path="/health/",
        headers={"host": "attacker.example", "origin": "http://attacker.example"},
        method="GET",
    )

    assert status == 403
    assert body == b"Invalid Origin header"
    assert app.called is False


def test_health_exemption_only_applies_to_probe_methods():
    app = RecordingApp()

    status, body = send_asgi_request(
        _middleware(app),
        path="/health",
        headers={"host": "attacker.example", "origin": "http://attacker.example"},
        method="POST",
    )

    assert status == 403
    assert body == b"Invalid Origin header"
    assert app.called is False


def test_non_http_scopes_pass_through():
    """FastMCP's MCP transports do not use non-HTTP ASGI scopes."""
    app = RecordingApp()
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

    def test_unset_uses_loopback_host_defaults(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", raising=False)
        monkeypatch.delenv("CLICKHOUSE_MCP_BIND_HOST", raising=False)

        config = MCPServerConfig()

        assert config.allowed_hosts == [
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            "[::1]",
            "[::1]:*",
        ]
        assert config.allowed_origins == []

    def test_comma_separated_values_are_split_and_trimmed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", " localhost:8000 , 127.0.0.1:8000 ,, ")
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", "http://localhost:3000")

        config = MCPServerConfig()

        assert config.allowed_hosts == ["localhost:8000", "127.0.0.1:8000"]
        assert config.allowed_origins == ["http://localhost:3000"]

    @pytest.mark.parametrize("value", ["", "  , "])
    def test_explicit_empty_hosts_raise(self, monkeypatch: pytest.MonkeyPatch, value: str):
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", value)

        with pytest.raises(ValueError, match="contains no Host values"):
            MCPServerConfig().allowed_hosts

    @pytest.mark.parametrize("bind_host", ["0.0.0.0", "::"])
    def test_wildcard_bind_requires_explicit_hosts(
        self, monkeypatch: pytest.MonkeyPatch, bind_host: str
    ):
        monkeypatch.delenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.setenv("CLICKHOUSE_MCP_BIND_HOST", bind_host)

        with pytest.raises(ValueError, match="must contain the public host"):
            MCPServerConfig().allowed_hosts

    def test_concrete_bind_derives_host_and_port(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.setenv("CLICKHOUSE_MCP_BIND_HOST", "mcp.internal")
        monkeypatch.setenv("CLICKHOUSE_MCP_BIND_PORT", "4200")

        assert MCPServerConfig().allowed_hosts == ["mcp.internal:4200"]


class TestMiddlewareFactory:
    """transport_security_middleware wiring."""

    def test_default_middleware_uses_secure_loopback_hosts(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", raising=False)
        monkeypatch.delenv("CLICKHOUSE_MCP_BIND_HOST", raising=False)

        built = transport_security_middleware(MCPServerConfig())

        assert len(built) == 1
        assert built[0].kwargs["allowed_hosts"] == [
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            "[::1]",
            "[::1]:*",
        ]
        assert built[0].kwargs["allowed_origins"] == []

    def test_middleware_is_built_from_configured_hosts(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "localhost:8000")
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", "http://localhost:3000")

        built = transport_security_middleware(MCPServerConfig())

        assert len(built) == 1
        assert isinstance(built[0], Middleware)
        assert built[0].cls is DNSRebindingProtectionMiddleware
        assert built[0].kwargs["allowed_hosts"] == ["localhost:8000"]
        assert built[0].kwargs["allowed_origins"] == ["http://localhost:3000"]

    def test_origins_are_configured_independently_of_explicit_hosts(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("CLICKHOUSE_MCP_BIND_HOST", raising=False)
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", "http://localhost:3000")

        built = transport_security_middleware(MCPServerConfig())

        assert built[0].kwargs["allowed_origins"] == ["http://localhost:3000"]
