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

    A reverse proxy normally rewrites Host to the name of the upstream it is
    forwarding to and carries the name the client actually used in
    X-Forwarded-Host, so behind one the allow list can otherwise only hold
    infrastructure-internal names. `trust_forwarded_host` validates that header
    instead. It is off by default because any client can send the header
    directly; turning it on is the operator asserting that a proxy in front
    overwrites it, the same assertion uvicorn's --proxy-headers asks for.
    """

    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Iterable[str],
        allowed_origins: Iterable[str] = (),
        trust_forwarded_host: bool = False,
    ) -> None:
        self.app = app
        self.allowed_hosts = list(allowed_hosts)
        self.allowed_origins = list(allowed_origins)
        self.trust_forwarded_host = trust_forwarded_host

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

        host = self._effective_host(headers)
        if not _matches(host or "", self.allowed_hosts):
            logger.warning("Rejected request with Host header %r", host)
            self._explain_rejection(headers)
            return PlainTextResponse("Invalid Host header", status_code=421)

        return None

    def _effective_host(self, headers: Headers) -> Optional[str]:
        """Return the host name to validate for this request.

        With a proxy chain the leftmost X-Forwarded-Host value is the name the
        original client used; the rest are hops added on the way in.
        """
        if self.trust_forwarded_host:
            forwarded = headers.get("x-forwarded-host")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return headers.get("host")

    def _explain_rejection(self, headers: Headers) -> None:
        """Name the setting that resolves a rejection caused by a proxy.

        Health stays green while every MCP call fails, so a rejection behind a
        proxy reads as a partial outage rather than as configuration.
        """
        if self.trust_forwarded_host or not headers.get("x-forwarded-host"):
            return
        logger.warning(
            "The request carried X-Forwarded-Host %r, so a reverse proxy rewrote Host. "
            "Set CLICKHOUSE_MCP_TRUST_FORWARDED_HOST=true to validate the forwarded name "
            "instead, or add the proxy's upstream name to CLICKHOUSE_MCP_ALLOWED_HOSTS.",
            headers.get("x-forwarded-host"),
        )


def transport_security_middleware(mcp_config) -> List[Middleware]:
    """Build required transport security middleware from server configuration."""
    allowed_hosts = mcp_config.allowed_hosts
    allowed_origins = mcp_config.allowed_origins

    trust_forwarded_host = mcp_config.trust_forwarded_host

    logger.info(
        "HTTP transport validation configured for %d host(s) and %d browser origin(s)",
        len(allowed_hosts),
        len(allowed_origins),
    )
    if trust_forwarded_host:
        logger.warning(
            "CLICKHOUSE_MCP_TRUST_FORWARDED_HOST is enabled, so X-Forwarded-Host is "
            "validated in place of Host. Only use this when a reverse proxy that "
            "overwrites the header is the sole route to this server."
        )
    return [
        Middleware(
            DNSRebindingProtectionMiddleware,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            trust_forwarded_host=trust_forwarded_host,
        )
    ]
