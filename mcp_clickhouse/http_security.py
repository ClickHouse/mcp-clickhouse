"""Host and Origin validation for HTTP and SSE transports."""

import logging
from typing import Iterable, List, Optional

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
        if scope["type"] != "http" or (
            scope.get("path"), scope.get("method")
        ) in _EXEMPT_REQUESTS:
            await self.app(scope, receive, send)
            return

        rejection = self._rejection_for(Headers(scope=scope))
        if rejection is not None:
            await rejection(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _rejection_for(self, headers: Headers) -> Optional[PlainTextResponse]:
        """Return the response to send instead of forwarding, or None to allow."""
        origin = headers.get("origin")
        if origin is not None and not _matches(origin, self.allowed_origins):
            logger.warning("Rejected request with Origin header %r", origin)
            return PlainTextResponse("Invalid Origin header", status_code=403)

        host = headers.get("host")
        if not _matches(host or "", self.allowed_hosts):
            logger.warning("Rejected request with Host header %r", host)
            return PlainTextResponse("Invalid Host header", status_code=421)

        return None


def transport_security_middleware(mcp_config) -> List[Middleware]:
    """Build required transport security middleware from server configuration."""
    allowed_hosts = mcp_config.allowed_hosts
    allowed_origins = mcp_config.allowed_origins

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
        )
    ]
