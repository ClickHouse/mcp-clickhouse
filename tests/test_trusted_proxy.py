"""Tests for Host validation behind a trusted reverse proxy."""

import asyncio
import logging

import pytest

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


def _send_request(middleware, headers=None, path="/mcp"):
    raw_headers = [
        (name.lower().encode(), value.encode()) for name, value in (headers or {}).items()
    ]
    scope = {"type": "http", "method": "POST", "path": path, "headers": raw_headers}
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(middleware(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    return status


def _middleware(app, allowed_hosts=("mcp.example.com",), trust_forwarded_host=False, **kwargs):
    return DNSRebindingProtectionMiddleware(
        app,
        allowed_hosts=allowed_hosts,
        trust_forwarded_host=trust_forwarded_host,
        **kwargs,
    )


# What a reverse proxy actually puts on the wire. nginx rewrites Host to the
# upstream it forwards to unless explicitly configured otherwise.
PROXY_HEADERS = {"host": "internal-upstream", "x-forwarded-host": "mcp.example.com"}


class TestWithoutTrust:
    """Default behavior is unchanged: only the raw Host header is validated."""

    def test_proxied_request_is_rejected(self):
        app = _RecordingApp()

        assert _send_request(_middleware(app), headers=PROXY_HEADERS) == 421
        assert app.called is False

    def test_forged_forwarded_host_does_not_bypass_validation(self):
        """A direct client can set the header, so it must not be believed."""
        app = _RecordingApp()
        headers = {"host": "attacker.example.com", "x-forwarded-host": "mcp.example.com"}

        assert _send_request(_middleware(app), headers=headers) == 421
        assert app.called is False

    def test_direct_request_still_passes(self):
        app = _RecordingApp()

        assert _send_request(_middleware(app), headers={"host": "mcp.example.com"}) == 200


class TestWithTrust:
    """With the proxy asserted, the forwarded name is the one validated."""

    def test_proxied_request_is_accepted(self):
        app = _RecordingApp()
        middleware = _middleware(app, trust_forwarded_host=True)

        assert _send_request(middleware, headers=PROXY_HEADERS) == 200
        assert app.called is True

    def test_leftmost_value_of_a_proxy_chain_is_used(self):
        """The leftmost entry is the name the original client used."""
        app = _RecordingApp()
        headers = {
            "host": "internal-upstream",
            "x-forwarded-host": "mcp.example.com, edge-1.internal, edge-2.internal",
        }

        assert _send_request(_middleware(app, trust_forwarded_host=True), headers=headers) == 200

    def test_whitespace_around_chain_values_is_ignored(self):
        app = _RecordingApp()
        headers = {"host": "internal-upstream", "x-forwarded-host": "  mcp.example.com  "}

        assert _send_request(_middleware(app, trust_forwarded_host=True), headers=headers) == 200

    def test_disallowed_forwarded_host_is_still_rejected(self):
        """Trusting the header is not the same as accepting any value in it."""
        app = _RecordingApp()
        headers = {"host": "internal-upstream", "x-forwarded-host": "attacker.example.com"}

        assert _send_request(_middleware(app, trust_forwarded_host=True), headers=headers) == 421
        assert app.called is False

    def test_falls_back_to_host_when_the_header_is_absent(self):
        """The same server still answers requests that did not pass a proxy."""
        app = _RecordingApp()
        middleware = _middleware(app, trust_forwarded_host=True)

        assert _send_request(middleware, headers={"host": "mcp.example.com"}) == 200

    def test_empty_forwarded_header_falls_back_to_host(self):
        app = _RecordingApp()
        headers = {"host": "mcp.example.com", "x-forwarded-host": ""}

        assert _send_request(_middleware(app, trust_forwarded_host=True), headers=headers) == 200

    def test_origin_validation_is_unaffected(self):
        """Proxies pass Origin through unchanged, so it is not remapped."""
        app = _RecordingApp()
        middleware = _middleware(
            app, trust_forwarded_host=True, allowed_origins=["https://app.example.com"]
        )
        headers = dict(PROXY_HEADERS, origin="https://attacker.example.com")

        assert _send_request(middleware, headers=headers) == 403


class TestDiagnostic:
    """A rejection caused by a proxy names the setting that resolves it."""

    def test_rejection_behind_a_proxy_is_explained(self, caplog):
        app = _RecordingApp()

        with caplog.at_level(logging.WARNING, logger="mcp-clickhouse"):
            _send_request(_middleware(app), headers=PROXY_HEADERS)

        assert "CLICKHOUSE_MCP_TRUST_FORWARDED_HOST" in caplog.text
        assert "mcp.example.com" in caplog.text

    def test_no_explanation_without_a_forwarded_header(self):
        """An ordinary rebinding attempt is not a proxy misconfiguration."""
        app = _RecordingApp()
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("mcp-clickhouse")
        logger.addHandler(handler)
        try:
            _send_request(_middleware(app), headers={"host": "attacker.example.com"})
        finally:
            logger.removeHandler(handler)

        assert not any("TRUST_FORWARDED_HOST" in r.getMessage() for r in records)

    def test_no_explanation_when_trust_is_already_enabled(self, caplog):
        app = _RecordingApp()
        headers = {"host": "internal-upstream", "x-forwarded-host": "attacker.example.com"}

        with caplog.at_level(logging.WARNING, logger="mcp-clickhouse"):
            _send_request(_middleware(app, trust_forwarded_host=True), headers=headers)

        assert "CLICKHOUSE_MCP_TRUST_FORWARDED_HOST=true" not in caplog.text


class TestConfig:
    """CLICKHOUSE_MCP_TRUST_FORWARDED_HOST parsing and wiring."""

    def test_defaults_to_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CLICKHOUSE_MCP_TRUST_FORWARDED_HOST", raising=False)

        assert MCPServerConfig().trust_forwarded_host is False

    @pytest.mark.parametrize(
        "value, expected",
        [("true", True), ("TRUE", True), ("false", False), ("1", False), ("", False)],
    )
    def test_parsing(self, monkeypatch: pytest.MonkeyPatch, value, expected):
        monkeypatch.setenv("CLICKHOUSE_MCP_TRUST_FORWARDED_HOST", value)

        assert MCPServerConfig().trust_forwarded_host is expected

    def test_setting_reaches_the_middleware(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
        monkeypatch.setenv("CLICKHOUSE_MCP_TRUST_FORWARDED_HOST", "true")

        built = transport_security_middleware(MCPServerConfig())

        assert built[0].kwargs["trust_forwarded_host"] is True
