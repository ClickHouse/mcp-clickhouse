import inspect
import logging
from contextvars import ContextVar
from ipaddress import IPv4Network, IPv6Network
from typing import Any, List, Optional

from fastmcp import FastMCP, settings as fastmcp_settings
from fastmcp.server.http import create_sse_app
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mcp_clickhouse.auth import _resolve_auth
from mcp_clickhouse.http_security import transport_security_middleware
from mcp_clickhouse.mcp_env import TransportType, get_mcp_config

logger = logging.getLogger("mcp-clickhouse")

_BUILTIN_HTTP_RAW_CLIENT = ContextVar("builtin_http_raw_client", default=False)
_SSE_DEPRECATION_MESSAGE = (
    "The legacy HTTP+SSE transport is deprecated. Use "
    "CLICKHOUSE_MCP_SERVER_TRANSPORT=http for Streamable HTTP."
)


def _proxy_header_trusted_hosts(
    trusted_proxies: List[IPv4Network | IPv6Network],
) -> List[str]:
    """Uvicorn trusted_hosts entries with IPv4-mapped IPv6 forms added.

    Uvicorn compares the raw peer without unmapping, so on a dual-stack bind an
    IPv4 proxy seen as ::ffff:a.b.c.d only matches the mapped form.
    """
    trusted_hosts = []
    for network in trusted_proxies:
        trusted_hosts.append(str(network))
        if isinstance(network, IPv4Network):
            trusted_hosts.append(f"::ffff:{network.network_address}/{network.prefixlen + 96}")
    return trusted_hosts


class ClickHouseFastMCP(FastMCP):
    """FastMCP server that secures every constructed HTTP transport app."""

    def http_app(
        self,
        *args: Any,
        raw_client_address_preserved: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Create an authenticated HTTP app with Host and Origin validation."""
        upstream_http_app = super().http_app
        bound_args = inspect.signature(upstream_http_app).bind_partial(*args, **kwargs)
        transport = bound_args.arguments.get("transport", TransportType.HTTP.value)
        if transport == TransportType.SSE.value:
            logger.warning(_SSE_DEPRECATION_MESSAGE)
        mcp_config = get_mcp_config()
        trusted_proxies = mcp_config.trusted_proxies
        if _BUILTIN_HTTP_RAW_CLIENT.get():
            # Consume the runner's single app construction so later calls in
            # the same context do not inherit the assertion.
            _BUILTIN_HTTP_RAW_CLIENT.set(False)
            raw_client_address_preserved = True
        if trusted_proxies and not raw_client_address_preserved:
            raise ValueError(
                "CLICKHOUSE_MCP_TRUSTED_PROXIES requires a raw ASGI client address. "
                "Disable proxy-header processing in the outer ASGI server and pass "
                "raw_client_address_preserved=True."
            )

        auth_kwargs = _resolve_auth(mcp_config, transport=transport)
        original_auth = self.auth
        if "auth" in auth_kwargs:
            app_auth = auth_kwargs["auth"]
        elif original_auth is None:
            raise ValueError("FASTMCP_SERVER_AUTH did not create an authentication provider")
        else:
            app_auth = original_auth

        bound_args.arguments["host_origin_protection"] = False
        self.auth = app_auth
        try:
            app = upstream_http_app(*bound_args.args, **bound_args.kwargs)
        finally:
            self.auth = original_auth
        if getattr(app.state, "path", None) == "/health":
            raise ValueError(
                "MCP transport path cannot be /health because that path is reserved "
                "for the public health endpoint"
            )
        if trusted_proxies:
            app.add_middleware(
                ProxyHeadersMiddleware,
                trusted_hosts=_proxy_header_trusted_hosts(trusted_proxies),
            )
        for configured_middleware in transport_security_middleware(mcp_config):
            app.add_middleware(configured_middleware.cls, **configured_middleware.kwargs)
        return app

    def sse_app(
        self,
        path: Optional[str] = None,
        message_path: Optional[str] = None,
        middleware: Optional[list] = None,
        *,
        raw_client_address_preserved: bool = False,
    ) -> Any:
        """Create a secured legacy SSE app."""
        logger.warning(_SSE_DEPRECATION_MESSAGE)
        mcp_config = get_mcp_config()
        trusted_proxies = mcp_config.trusted_proxies
        if trusted_proxies and not raw_client_address_preserved:
            raise ValueError(
                "CLICKHOUSE_MCP_TRUSTED_PROXIES requires a raw ASGI client address. "
                "Disable proxy-header processing in the outer ASGI server and pass "
                "raw_client_address_preserved=True."
            )

        sse_path = path or fastmcp_settings.sse_path
        resolved_message_path = message_path or fastmcp_settings.message_path
        if sse_path == "/health" or resolved_message_path == "/health":
            raise ValueError(
                "MCP transport path cannot be /health because that path is reserved "
                "for the public health endpoint"
            )
        auth_kwargs = _resolve_auth(mcp_config, transport=TransportType.SSE.value)
        app_auth = auth_kwargs.get("auth", self.auth)
        if app_auth is None and "auth" not in auth_kwargs:
            raise ValueError("FASTMCP_SERVER_AUTH did not create an authentication provider")

        app = create_sse_app(
            server=self,
            message_path=resolved_message_path,
            sse_path=sse_path,
            auth=app_auth,
            debug=fastmcp_settings.debug,
            middleware=middleware,
        )
        if trusted_proxies:
            app.add_middleware(
                ProxyHeadersMiddleware,
                trusted_hosts=_proxy_header_trusted_hosts(trusted_proxies),
            )
        for configured_middleware in transport_security_middleware(mcp_config):
            app.add_middleware(configured_middleware.cls, **configured_middleware.kwargs)
        return app

    def streamable_http_app(
        self,
        path: Optional[str] = None,
        middleware: Optional[list] = None,
        *,
        raw_client_address_preserved: bool = False,
    ) -> Any:
        """Create a secured streamable HTTP app.

        Deprecated upstream; prefer http_app().
        """
        return self.http_app(
            path=path,
            middleware=middleware,
            transport=TransportType.HTTP.value,
            raw_client_address_preserved=raw_client_address_preserved,
        )

    async def run_http_async(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Run HTTP with Host validation before trusted proxy header processing."""
        upstream_run_http = super().run_http_async
        trusted_proxies = get_mcp_config().trusted_proxies
        if not trusted_proxies:
            await upstream_run_http(*args, **kwargs)
            return

        upstream_signature = inspect.signature(upstream_run_http)
        if "uvicorn_config" not in upstream_signature.parameters:
            raise RuntimeError(
                "CLICKHOUSE_MCP_TRUSTED_PROXIES requires a FastMCP version whose "
                "HTTP runner supports uvicorn_config"
            )
        bound_args = upstream_signature.bind_partial(*args, **kwargs)
        inner_uvicorn_config = dict(bound_args.arguments.get("uvicorn_config") or {})
        if inner_uvicorn_config.get("proxy_headers"):
            raise ValueError(
                "uvicorn_config['proxy_headers'] must be false when "
                "CLICKHOUSE_MCP_TRUSTED_PROXIES is configured"
            )
        inner_uvicorn_config["proxy_headers"] = False
        bound_args.arguments["uvicorn_config"] = inner_uvicorn_config
        token = _BUILTIN_HTTP_RAW_CLIENT.set(True)
        try:
            await upstream_run_http(*bound_args.args, **bound_args.kwargs)
        finally:
            _BUILTIN_HTTP_RAW_CLIENT.reset(token)
