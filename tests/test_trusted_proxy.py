"""Tests for Host validation behind a trusted reverse proxy."""

import asyncio
from ipaddress import ip_network
import logging

import pytest

from mcp_clickhouse.http_security import (
    DNSRebindingProtectionMiddleware,
    transport_security_middleware,
)
from mcp_clickhouse.mcp_env import MCPServerConfig
from tests.helpers import RecordingApp, send_asgi_request_async


async def _send_request_async(middleware, *, client=("203.0.113.10", 1234), **kwargs):
    """Status of one request; the scope always carries a client, None for a missing peer."""
    status, _ = await send_asgi_request_async(
        middleware, client=client, include_client=True, **kwargs
    )
    return status


def _send_request(middleware, **kwargs):
    return asyncio.run(_send_request_async(middleware, **kwargs))


def _middleware(app, trusted_proxies=(), **kwargs):
    return DNSRebindingProtectionMiddleware(
        app,
        allowed_hosts=kwargs.pop("allowed_hosts", ("mcp.example.com",)),
        trusted_proxies=[ip_network(value) for value in trusted_proxies],
        **kwargs,
    )


PROXY_HEADERS = {"host": "internal-upstream", "x-forwarded-host": "mcp.example.com"}


class TestTrustedPeerSelection:
    @pytest.mark.parametrize(
        "trusted_proxy,client",
        [
            ("10.0.0.8", ("10.0.0.8", 1234)),
            ("10.0.0.0/24", ("10.0.0.8", 1234)),
            ("2001:db8::8", ("2001:db8::8", 1234)),
            ("2001:db8::/64", ("2001:db8::8", 1234)),
        ],
    )
    def test_exact_and_cidr_proxy_peers_are_trusted(self, trusted_proxy, client):
        app = RecordingApp()

        status = _send_request(
            _middleware(app, trusted_proxies=(trusted_proxy,)),
            headers=PROXY_HEADERS,
            client=client,
        )

        assert status == 200
        assert len(app.scopes) == 1

    @pytest.mark.parametrize("trusted_proxy", ["10.20.0.8", "10.20.0.0/24"])
    def test_ipv4_mapped_peer_matches_ipv4_entries(self, trusted_proxy):
        app = RecordingApp()

        status = _send_request(
            _middleware(app, trusted_proxies=(trusted_proxy,)),
            headers=PROXY_HEADERS,
            client=("::ffff:10.20.0.8", 1234),
        )

        assert status == 200
        assert len(app.scopes) == 1

    @pytest.mark.parametrize("client", [("10.0.1.8", 1234), ("not-an-ip", 1234), None])
    def test_untrusted_or_missing_peer_ignores_forwarded_host(self, client):
        app = RecordingApp()

        status = _send_request(
            _middleware(app, trusted_proxies=("10.0.0.0/24",)),
            headers=PROXY_HEADERS,
            client=client,
        )

        assert status == 421
        assert app.scopes == []

    def test_untrusted_peer_cannot_bypass_raw_host_validation(self):
        headers = {"host": "attacker.example.com", "x-forwarded-host": "mcp.example.com"}

        status = _send_request(
            _middleware(RecordingApp(), trusted_proxies=("10.0.0.0/24",)),
            headers=headers,
            client=("203.0.113.10", 1234),
        )

        assert status == 421

    def test_untrusted_peer_with_allowed_raw_host_ignores_malformed_forwarded_host(self):
        headers = {"host": "mcp.example.com", "x-forwarded-host": "bad, list"}

        status = _send_request(
            _middleware(RecordingApp(), trusted_proxies=("10.0.0.0/24",)),
            headers=headers,
            client=("203.0.113.10", 1234),
        )

        assert status == 200


class TestForwardedHostValidation:
    @pytest.mark.parametrize(
        "forwarded_headers",
        [
            [("x-forwarded-host", "mcp.example.com"), ("x-forwarded-host", "attacker")],
            [("x-forwarded-host", "attacker"), ("x-forwarded-host", "mcp.example.com")],
            [("x-forwarded-host", "mcp.example.com, attacker")],
            [("x-forwarded-host", "")],
            [("x-forwarded-host", "   ")],
        ],
        ids=["duplicate-allowed-first", "duplicate-allowed-last", "comma-list", "empty", "blank"],
    )
    def test_ambiguous_forwarded_host_is_rejected(self, forwarded_headers):
        headers = [("host", "mcp.example.com"), *forwarded_headers]

        status = _send_request(
            _middleware(RecordingApp(), trusted_proxies=("10.0.0.8",)),
            headers=headers,
            client=("10.0.0.8", 1234),
        )

        assert status == 421

    def test_forwarded_host_may_have_outer_whitespace(self):
        headers = {"host": "internal-upstream", "x-forwarded-host": "  mcp.example.com  "}

        status = _send_request(
            _middleware(RecordingApp(), trusted_proxies=("10.0.0.8",)),
            headers=headers,
            client=("10.0.0.8", 1234),
        )

        assert status == 200

    def test_missing_forwarded_host_falls_back_to_raw_host(self):
        status = _send_request(
            _middleware(RecordingApp(), trusted_proxies=("10.0.0.8",)),
            headers={"host": "mcp.example.com"},
            client=("10.0.0.8", 1234),
        )

        assert status == 200

    def test_invalid_origin_wins_over_invalid_forwarded_host(self):
        headers = [
            ("host", "attacker"),
            ("x-forwarded-host", ""),
            ("origin", "https://attacker.example.com"),
        ]

        status = _send_request(
            _middleware(
                RecordingApp(),
                trusted_proxies=("10.0.0.8",),
                allowed_origins=("https://app.example.com",),
            ),
            headers=headers,
            client=("10.0.0.8", 1234),
        )

        assert status == 403

    def test_rejection_does_not_log_forwarded_value(self, caplog):
        forwarded_value = "do-not-log.example.com"
        headers = {"host": "internal-upstream", "x-forwarded-host": forwarded_value}

        with caplog.at_level(logging.WARNING, logger="mcp-clickhouse"):
            status = _send_request(
                _middleware(RecordingApp(), trusted_proxies=("10.0.0.8",)),
                headers=headers,
                client=("10.0.0.8", 1234),
            )

        assert status == 421
        assert forwarded_value not in caplog.text

    def test_concurrent_requests_do_not_share_peer_state(self):
        middleware = _middleware(RecordingApp(), trusted_proxies=("10.0.0.0/24",))

        async def run_requests():
            return await asyncio.gather(
                _send_request_async(
                    middleware,
                    headers=PROXY_HEADERS,
                    client=("10.0.0.8", 1234),
                ),
                _send_request_async(
                    middleware,
                    headers=PROXY_HEADERS,
                    client=("203.0.113.8", 1234),
                ),
            )

        assert asyncio.run(run_requests()) == [200, 421]


class TestConfig:
    def test_defaults_to_no_trusted_proxies(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", raising=False)

        assert MCPServerConfig().trusted_proxies == []

    def test_parses_exact_addresses_and_cidrs(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(
            "CLICKHOUSE_MCP_TRUSTED_PROXIES",
            "127.0.0.1, 10.0.0.0/24, ::1, 2001:db8::/64",
        )

        assert MCPServerConfig().trusted_proxies == [
            ip_network("127.0.0.1"),
            ip_network("10.0.0.0/24"),
            ip_network("::1"),
            ip_network("2001:db8::/64"),
        ]

    @pytest.mark.parametrize(
        "value",
        ["proxy.internal", "10.0.0.1/24", "127.0.0.1,invalid"],
    )
    def test_rejects_invalid_values(self, monkeypatch: pytest.MonkeyPatch, value):
        monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", value)

        with pytest.raises(ValueError, match="Invalid IP address or CIDR"):
            MCPServerConfig().trusted_proxies

    @pytest.mark.parametrize("value", ["fe80::1%eth0", "fe80::%en0/64"])
    def test_rejects_scoped_ipv6_values(self, monkeypatch: pytest.MonkeyPatch, value):
        monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", value)

        with pytest.raises(ValueError, match="Scoped IPv6 addresses are not supported"):
            MCPServerConfig().trusted_proxies

    @pytest.mark.parametrize("value", ["*", "0.0.0.0/0", "::/0", "::ffff:0:0/96"])
    def test_rejects_trust_all_values(self, monkeypatch: pytest.MonkeyPatch, value):
        monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", value)

        with pytest.raises(ValueError, match="cannot trust every address"):
            MCPServerConfig().trusted_proxies

    def test_rejects_supernets_of_the_ipv4_mapped_range(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "::/1")

        with pytest.raises(ValueError, match="IPv4-mapped range"):
            MCPServerConfig().trusted_proxies

    def test_accepts_ipv6_networks_outside_the_mapped_range(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "8000::/1")

        assert MCPServerConfig().trusted_proxies == [ip_network("8000::/1")]

    def test_normalizes_ipv4_mapped_networks_to_ipv4(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "::ffff:10.20.0.0/120")

        parsed = MCPServerConfig().trusted_proxies

        assert parsed == [ip_network("10.20.0.0/24")]
        middleware = DNSRebindingProtectionMiddleware(
            RecordingApp(),
            allowed_hosts=("mcp.example.com",),
            trusted_proxies=parsed,
        )
        status = _send_request(middleware, headers=PROXY_HEADERS, client=("10.20.0.8", 1234))
        assert status == 200

    def test_setting_reaches_the_middleware(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "mcp.example.com")
        monkeypatch.setenv("CLICKHOUSE_MCP_TRUSTED_PROXIES", "10.0.0.0/24")

        built = transport_security_middleware(MCPServerConfig())

        assert built[0].kwargs["trusted_proxies"] == [ip_network("10.0.0.0/24")]
