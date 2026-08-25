"""Host and Origin validation for the HTTP and SSE transports.

A browser page cannot normally read a response from a server on the developer's
own machine, but DNS rebinding sidesteps that: the attacker points a hostname
they control at 127.0.0.1, so by the browser's rules the page is talking to its
own origin while the request actually lands on the local MCP server. Checking
that the Host header names an address this deployment answers for closes that
path, because the rebound request still carries the attacker's hostname.

The MCP SDK ships the same check in `mcp.server.transport_security`, but FastMCP
never constructs the settings object that turns it on, so a FastMCP server does
not perform it. This module supplies the check as ASGI middleware instead.
"""

import logging
from typing import Iterable, List, Optional

from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("mcp-clickhouse")

# The liveness probe is already unauthenticated and returns no information about
# the deployment, and probes reach it under whatever host name the orchestrator
# uses, which an operator has no reason to know in advance.
_EXEMPT_PATHS = frozenset({"/health"})


def _matches(value: str, patterns: Iterable[str]) -> bool:
    """Report whether `value` is allow-listed.

    A pattern is either an exact value or a `host:*` form that accepts the host
    on any port, matching the pattern language of the MCP SDK.
    """
    for pattern in patterns:
        if pattern == value:
            return True
        if pattern.endswith(":*") and value.startswith(pattern[:-1]):
            return True
    return False


class DNSRebindingProtectionMiddleware:
    """Reject HTTP requests whose Host or Origin header is not allow-listed.

    A missing Host header is rejected: every HTTP/1.1 client sends one, so its
    absence is not a request shape worth serving. A missing Origin header is
    accepted, because non-browser clients omit it and they are not the traffic
    this guards against.
    """

    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Iterable[str],
        allowed_origins: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.allowed_hosts = list(allowed_hosts)
        self.allowed_origins = list(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        rejection = self._rejection_for(Headers(scope=scope))
        if rejection is not None:
            await rejection(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _rejection_for(self, headers: Headers) -> Optional[PlainTextResponse]:
        """Return the response to send instead of forwarding, or None to allow."""
        host = headers.get("host")
        if not _matches(host or "", self.allowed_hosts):
            logger.warning("Rejected request with Host header %r", host)
            # 421 Misdirected Request: this server does not answer for that host.
            return PlainTextResponse("Invalid Host header", status_code=421)

        origin = headers.get("origin")
        if origin and not _matches(origin, self.allowed_origins):
            logger.warning("Rejected request with Origin header %r", origin)
            return PlainTextResponse("Invalid Origin header", status_code=403)

        return None


def transport_security_middleware(mcp_config) -> List[Middleware]:
    """Build the middleware list for the configured transport.

    Returns an empty list when no allowed hosts are configured, which leaves the
    server behaving exactly as it did before this check existed.
    """
    allowed_hosts = mcp_config.allowed_hosts
    allowed_origins = mcp_config.allowed_origins

    if not allowed_hosts:
        if allowed_origins:
            raise ValueError(
                "CLICKHOUSE_MCP_ALLOWED_ORIGINS is set but CLICKHOUSE_MCP_ALLOWED_HOSTS "
                "is not. Origin checking runs as part of the Host check, so set "
                "CLICKHOUSE_MCP_ALLOWED_HOSTS as well or the origins are never consulted."
            )
        port = mcp_config.bind_port
        logger.warning(
            "CLICKHOUSE_MCP_ALLOWED_HOSTS is not set, so the server answers for any Host "
            "header and a browser page can reach it through DNS rebinding. Set it to the "
            "host:port that clients connect to, for example "
            "CLICKHOUSE_MCP_ALLOWED_HOSTS=127.0.0.1:%s,localhost:%s",
            port,
            port,
        )
        return []

    logger.info(
        "Host header validation enabled for %d allowed host(s) and %d allowed origin(s)",
        len(allowed_hosts),
        len(allowed_origins),
    )
    return [
        Middleware(
            DNSRebindingProtectionMiddleware,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )
    ]
