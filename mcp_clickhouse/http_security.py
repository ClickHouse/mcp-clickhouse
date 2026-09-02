"""Host and Origin validation for the HTTP transport."""

import logging
from ipaddress import IPv4Network, IPv6Address, IPv6Network, ip_address
from typing import Iterable, List, Optional, Tuple

from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("mcp-clickhouse")

# Health is a public operational route outside MCP transport validation.
_EXEMPT_REQUESTS = frozenset({("/health", "GET"), ("/health", "HEAD")})


def _matches(value: str, patterns: Iterable[str]) -> bool:
    """Report whether `value` is allow-listed.

    A pattern is either an exact value or a `host:*` form that accepts the host
    on any port, matching the pattern language of the MCP SDK.
    """
    value = value.lower()
    for pattern in patterns:
        pattern = pattern.lower()
        if pattern == value:
            return True
        if pattern.endswith(":*"):
            host, separator, port = value.rpartition(":")
            if separator and host == pattern[:-2] and port.isdigit():
                return True
    return False


class DNSRebindingProtectionMiddleware:
    """Reject HTTP requests with an invalid Origin or Host header.

    MCP requires validation of every present Origin header. Missing Origin
    headers remain valid for non-browser clients. Host validation is an
    additional DNS rebinding defense.

    X-Forwarded-Host is used only when the immediate network peer belongs to a
    configured trusted proxy network.
    """

    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Iterable[str],
        allowed_origins: Iterable[str] = (),
        trusted_proxies: Iterable[IPv4Network | IPv6Network] = (),
    ) -> None:
        self.app = app
        self.allowed_hosts = list(allowed_hosts)
        self.allowed_origins = list(allowed_origins)
        self.trusted_proxies = tuple(trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (
            scope.get("path"), scope.get("method")
        ) in _EXEMPT_REQUESTS:
            await self.app(scope, receive, send)
            return

        rejection = self._rejection_for(scope, Headers(scope=scope))
        if rejection is not None:
            await rejection(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _rejection_for(self, scope: Scope, headers: Headers) -> Optional[PlainTextResponse]:
        """Return the response to send instead of forwarding, or None to allow."""
        origin = headers.get("origin")
        if origin is not None and not _matches(origin, self.allowed_origins):
            logger.warning("Rejected request with Origin header %r", origin)
            return PlainTextResponse("Invalid Origin header", status_code=403)

        host, used_forwarded_host = self._effective_host(scope, headers)
        if not _matches(host or "", self.allowed_hosts):
            if used_forwarded_host:
                logger.warning("Rejected request with invalid X-Forwarded-Host header")
            else:
                logger.warning("Rejected request with Host header %r", host)
            return PlainTextResponse("Invalid Host header", status_code=421)

        return None

    def _effective_host(self, scope: Scope, headers: Headers) -> Tuple[Optional[str], bool]:
        """Return the Host value to validate and whether it came from a proxy."""
        if not self._is_trusted_peer(scope):
            return headers.get("host"), False

        forwarded_hosts = headers.getlist("x-forwarded-host")
        if not forwarded_hosts:
            return headers.get("host"), False
        if len(forwarded_hosts) != 1:
            return None, True

        forwarded_host = forwarded_hosts[0].strip()
        if not forwarded_host or "," in forwarded_host:
            return None, True
        return forwarded_host, True

    def _is_trusted_peer(self, scope: Scope) -> bool:
        """Report whether the raw ASGI client address is a configured proxy."""
        client = scope.get("client")
        if (
            not isinstance(client, (list, tuple))
            or not client
            or not isinstance(client[0], str)
        ):
            return False
        try:
            peer = ip_address(client[0])
        except ValueError:
            return False
        if isinstance(peer, IPv6Address):
            # Dual-stack binds present IPv4 peers as ::ffff:a.b.c.d.
            peer = peer.ipv4_mapped or peer
        return any(peer in network for network in self.trusted_proxies)


def transport_security_middleware(mcp_config) -> List[Middleware]:
    """Build required transport security middleware from server configuration."""
    allowed_hosts = mcp_config.allowed_hosts
    allowed_origins = mcp_config.allowed_origins
    trusted_proxies = mcp_config.trusted_proxies

    logger.info(
        "HTTP transport validation configured for %d host(s) and %d browser origin(s)",
        len(allowed_hosts),
        len(allowed_origins),
    )
    return [
        Middleware(
            DNSRebindingProtectionMiddleware,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            trusted_proxies=trusted_proxies,
        )
    ]
