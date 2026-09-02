import asyncio
import atexit
import concurrent.futures
import importlib
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from functools import wraps
from importlib.metadata import PackageNotFoundError, version as package_version
from ipaddress import IPv4Network, IPv6Network
from typing import Annotated, Any, Dict, List, Optional, Tuple

import clickhouse_connect
import simplejson
from cachetools import TTLCache
from clickhouse_connect.driver.binding import format_query_value
from clickhouse_connect.driver.exceptions import OperationalError
from dotenv import dotenv_values, load_dotenv
from fastmcp import FastMCP, settings as fastmcp_settings
from fastmcp.exceptions import ToolError
from fastmcp.prompts import Prompt
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_context
from fastmcp.server.http import create_sse_app
from fastmcp.tools import Tool
from fastmcp.utilities.auth import parse_scopes
from pydantic import Field, TypeAdapter
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mcp_clickhouse.chdb_prompt import CHDB_PROMPT
from mcp_clickhouse.http_security import transport_security_middleware
from mcp_clickhouse.mcp_env import TransportType, get_chdb_config, get_config, get_mcp_config
from mcp_clickhouse.skills_advisor import CLICKHOUSE_SERVER_INSTRUCTIONS


@dataclass
class Column:
    database: str
    table: str
    name: str
    column_type: str
    default_kind: Optional[str]
    default_expression: Optional[str]
    comment: Optional[str]


@dataclass
class Table:
    database: str
    name: str
    engine: str
    create_table_query: str
    dependencies_database: str
    dependencies_table: str
    engine_full: str
    sorting_key: str
    primary_key: str
    total_rows: int
    total_bytes: int
    total_bytes_uncompressed: int
    parts: int
    active_parts: int
    total_marks: int
    comment: Optional[str] = None
    columns: List[Column] = field(default_factory=list)


@dataclass
class _ClientCacheEntry:
    client: Any
    last_used: float
    active_users: int = 0
    retired: bool = False
    closed: bool = False


@dataclass
class _ActiveQueryState:
    query: str
    client_entry: Optional[_ClientCacheEntry] = None
    cancelled: bool = False


def _get_mcp_server_version() -> str:
    """Return the installed package version or an explicit unknown marker."""
    try:
        return package_version("mcp-clickhouse")
    except PackageNotFoundError:
        return "unknown"


def _is_legacy_auth_name(name: str) -> bool:
    return name.casefold().startswith("fastmcp_server_auth")


def _find_default_dotenv() -> str:
    """Find the nearest .env at or above the installed package directory."""
    directory = os.path.dirname(os.path.realpath(__file__))
    while True:
        env_file = os.path.join(directory, ".env")
        if os.path.isfile(env_file):
            return env_file
        parent = os.path.dirname(directory)
        if parent == directory:
            return ""
        directory = parent


def _load_default_dotenv() -> None:
    """Load repository settings while preserving process auth precedence."""
    auth_snapshot = {
        name: value
        for name, value in os.environ.items()
        if _is_legacy_auth_name(name)
    }
    auth_process_names = {name.casefold() for name in auth_snapshot}
    env_file_snapshot = {
        name: value
        for name, value in os.environ.items()
        if name.casefold() == "fastmcp_env_file"
    }
    try:
        load_dotenv(dotenv_path=_find_default_dotenv())
    finally:
        for name in tuple(os.environ):
            if name.casefold() == "fastmcp_env_file" or (
                _is_legacy_auth_name(name) and name.casefold() in auth_process_names
            ):
                del os.environ[name]
        os.environ.update(auth_snapshot)
        os.environ.update(env_file_snapshot)


MCP_SERVER_NAME = "mcp-clickhouse"
MCP_SERVER_VERSION = _get_mcp_server_version()
CLIENT_CONFIG_OVERRIDES_KEY = "clickhouse_client_config_overrides"
_CLIENT_CONFIG_OVERRIDES_UNSET = object()
_NESTED_CLIENT_CONFIG_KEYS = ("settings", "generic_args")
_REJECTED_ROLE_OVERRIDE_KEYS = ("role", "ch_role")

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(MCP_SERVER_NAME)

_load_default_dotenv()

_max_workers = get_mcp_config().max_workers
QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers)
_METADATA_MAX_WORKERS = max(1, min(4, _max_workers))
METADATA_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_METADATA_MAX_WORKERS)
CANCELLATION_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2)
HEALTH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_QUERY_CANCELLATION_WAIT_SECONDS = 1.0
_HEALTH_CHECK_TIMEOUT_SECONDS = 2.0

_CLIENT_CACHE_MAXSIZE = 64
_client_cache: OrderedDict[Tuple, _ClientCacheEntry] = OrderedDict()
_client_cache_lock = threading.Lock()
_CLIENT_IDLE_PING_THRESHOLD = 60

_active_queries: Dict[str, _ActiveQueryState] = {}
_active_queries_lock = threading.Lock()

_health_probe_future: Optional[concurrent.futures.Future] = None
_health_probe_lock = threading.Lock()
_logged_health_probe_futures: weakref.WeakSet[concurrent.futures.Future] = weakref.WeakSet()

_HTTP_TRANSPORTS = (TransportType.HTTP.value, "streamable-http", TransportType.SSE.value)
_BUILTIN_HTTP_RAW_CLIENT = ContextVar("builtin_http_raw_client", default=False)
_SSE_DEPRECATION_MESSAGE = (
    "The legacy HTTP+SSE transport is deprecated. Use "
    "CLICKHOUSE_MCP_SERVER_TRANSPORT=http for Streamable HTTP."
)

_LEGACY_AUTH_PROVIDER_ENV: dict[str, tuple[str, tuple[str, ...]]] = {
    "fastmcp.server.auth.providers.auth0.Auth0Provider": (
        "AUTH0_",
        (
            "config_url",
            "client_id",
            "client_secret",
            "audience",
            "base_url",
            "issuer_url",
            "redirect_path",
            "required_scopes",
            "allowed_client_redirect_uris",
            "jwt_signing_key",
        ),
    ),
    "fastmcp.server.auth.providers.aws.AWSCognitoProvider": (
        "AWS_COGNITO_",
        (
            "user_pool_id",
            "aws_region",
            "client_id",
            "client_secret",
            "base_url",
            "issuer_url",
            "redirect_path",
            "required_scopes",
            "allowed_client_redirect_uris",
            "jwt_signing_key",
        ),
    ),
    "fastmcp.server.auth.providers.azure.AzureProvider": (
        "AZURE_",
        (
            "client_id",
            "client_secret",
            "tenant_id",
            "identifier_uri",
            "base_url",
            "issuer_url",
            "redirect_path",
            "required_scopes",
            "additional_authorize_scopes",
            "allowed_client_redirect_uris",
            "jwt_signing_key",
            "base_authority",
        ),
    ),
    "fastmcp.server.auth.providers.descope.DescopeProvider": (
        "DESCOPEPROVIDER_",
        ("config_url", "project_id", "descope_base_url", "base_url", "required_scopes"),
    ),
    "fastmcp.server.auth.providers.discord.DiscordProvider": (
        "DISCORD_",
        (
            "client_id",
            "client_secret",
            "base_url",
            "issuer_url",
            "redirect_path",
            "required_scopes",
            "timeout_seconds",
            "allowed_client_redirect_uris",
            "jwt_signing_key",
        ),
    ),
    "fastmcp.server.auth.providers.github.GitHubProvider": (
        "GITHUB_",
        (
            "client_id",
            "client_secret",
            "base_url",
            "issuer_url",
            "redirect_path",
            "required_scopes",
            "timeout_seconds",
            "allowed_client_redirect_uris",
            "jwt_signing_key",
        ),
    ),
    "fastmcp.server.auth.providers.google.GoogleProvider": (
        "GOOGLE_",
        (
            "client_id",
            "client_secret",
            "base_url",
            "issuer_url",
            "redirect_path",
            "required_scopes",
            "timeout_seconds",
            "allowed_client_redirect_uris",
            "jwt_signing_key",
        ),
    ),
    "fastmcp.server.auth.providers.introspection.IntrospectionTokenVerifier": (
        "INTROSPECTION_",
        (
            "introspection_url",
            "client_id",
            "client_secret",
            "timeout_seconds",
            "required_scopes",
            "base_url",
        ),
    ),
    "fastmcp.server.auth.providers.jwt.JWTVerifier": (
        "JWT_",
        (
            "public_key",
            "jwks_uri",
            "issuer",
            "algorithm",
            "audience",
            "required_scopes",
            "base_url",
        ),
    ),
    "fastmcp.server.auth.providers.oci.OCIProvider": (
        "OCI_",
        (
            "config_url",
            "client_id",
            "client_secret",
            "audience",
            "base_url",
            "issuer_url",
            "redirect_path",
            "required_scopes",
            "allowed_client_redirect_uris",
            "jwt_signing_key",
        ),
    ),
    "fastmcp.server.auth.providers.scalekit.ScalekitProvider": (
        "SCALEKITPROVIDER_",
        ("environment_url", "resource_id", "base_url", "mcp_url", "required_scopes"),
    ),
    "fastmcp.server.auth.providers.supabase.SupabaseProvider": (
        "SUPABASE_",
        ("project_url", "base_url", "auth_route", "algorithm", "required_scopes"),
    ),
    "fastmcp.server.auth.providers.workos.WorkOSProvider": (
        "WORKOS_",
        (
            "client_id",
            "client_secret",
            "authkit_domain",
            "base_url",
            "issuer_url",
            "redirect_path",
            "required_scopes",
            "timeout_seconds",
            "allowed_client_redirect_uris",
            "jwt_signing_key",
        ),
    ),
    "fastmcp.server.auth.providers.workos.AuthKitProvider": (
        "AUTHKITPROVIDER_",
        ("authkit_domain", "base_url", "required_scopes"),
    ),
}
_AUTH_SCOPE_FIELDS = frozenset({"required_scopes", "additional_authorize_scopes"})
_AUTH_JSON_LIST_FIELDS = frozenset({"allowed_client_redirect_uris"})
_AUTH_INT_FIELDS = frozenset({"timeout_seconds"})
_AUTH_INT_ADAPTER = TypeAdapter(int)
_LEGACY_AUTH_SCOPE_DEFAULTS = {
    "fastmcp.server.auth.providers.auth0.Auth0Provider": ["openid"],
    "fastmcp.server.auth.providers.aws.AWSCognitoProvider": ["openid"],
    "fastmcp.server.auth.providers.discord.DiscordProvider": ["identify"],
    "fastmcp.server.auth.providers.github.GitHubProvider": ["user"],
    "fastmcp.server.auth.providers.google.GoogleProvider": ["openid"],
    "fastmcp.server.auth.providers.oci.OCIProvider": ["openid"],
}
_LEGACY_ZERO_TIMEOUT_DEFAULTS = frozenset(
    {
        "fastmcp.server.auth.providers.discord.DiscordProvider",
        "fastmcp.server.auth.providers.github.GitHubProvider",
        "fastmcp.server.auth.providers.google.GoogleProvider",
        "fastmcp.server.auth.providers.workos.WorkOSProvider",
    }
)


def _get_case_insensitive_value(
    values: Mapping[str, Optional[str]], env_name: str
) -> Optional[str]:
    normalized_name = env_name.casefold()
    matched_value = None
    for configured_name, value in values.items():
        if configured_name.casefold() == normalized_name:
            matched_value = value
    return matched_value


def _get_legacy_auth_file_values() -> dict[str, Optional[str]]:
    """Read FastMCP 2 auth settings without mutating the process environment."""
    env_file = _get_case_insensitive_value(os.environ, "FASTMCP_ENV_FILE")
    explicit_env_file = env_file is not None
    values = dotenv_values(env_file if explicit_env_file else ".env")
    return {
        name: value
        for name, value in values.items()
        if _is_legacy_auth_name(name)
        and (explicit_env_file or name.casefold() != "fastmcp_server_auth")
    }


def _get_legacy_auth_env(
    env_name: str,
    auth_file_values: Optional[Mapping[str, Optional[str]]] = None,
) -> Optional[str]:
    """Read a FastMCP 2 auth variable with process-first precedence."""
    process_value = _get_case_insensitive_value(os.environ, env_name)
    if process_value is not None:
        return process_value
    file_values = (
        auth_file_values
        if auth_file_values is not None
        else _get_legacy_auth_file_values()
    )
    return _get_case_insensitive_value(file_values, env_name)


def _parse_auth_provider_env_value(
    provider_path: str,
    field_name: str,
    env_name: str,
    value: str,
) -> Any:
    """Parse one legacy FastMCP auth provider environment value."""
    try:
        if field_name in _AUTH_SCOPE_FIELDS:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return parse_scopes(value)
            if parsed is None:
                return None
            if isinstance(parsed, str) or (
                isinstance(parsed, list)
                and all(isinstance(item, str) for item in parsed)
            ):
                return parse_scopes(parsed)
            raise ValueError
        if field_name in _AUTH_JSON_LIST_FIELDS:
            parsed = json.loads(value)
            if parsed is None:
                return None
            if not isinstance(parsed, list) or not all(
                isinstance(item, str) for item in parsed
            ):
                raise ValueError
            return parsed
        if (
            provider_path == "fastmcp.server.auth.providers.jwt.JWTVerifier"
            and field_name in {"issuer", "audience"}
        ):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return value
            if parsed is None:
                return None
            if isinstance(parsed, str):
                return parsed
            if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
                raise ValueError
            return parsed
        if field_name in _AUTH_INT_FIELDS:
            return _AUTH_INT_ADAPTER.validate_python(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError(
            f"Invalid {env_name} value for FastMCP auth provider {provider_path}"
        ) from None
    return value


def _load_fastmcp_auth_provider(
    provider_path: str,
    *,
    auth_file_values: Optional[Mapping[str, Optional[str]]] = None,
) -> AuthProvider:
    """Load a FastMCP 2 style auth provider configuration under FastMCP 4."""
    if auth_file_values is None:
        auth_file_values = _get_legacy_auth_file_values()

    module_name, separator, class_name = provider_path.rpartition(".")
    if not separator:
        raise ValueError("FASTMCP_SERVER_AUTH must be a full AuthProvider class path")
    try:
        provider_class = getattr(importlib.import_module(module_name), class_name)
    except (AttributeError, ImportError):
        raise ValueError(f"Could not import FASTMCP_SERVER_AUTH provider {provider_path}") from None
    if not inspect.isclass(provider_class) or not issubclass(provider_class, AuthProvider):
        raise ValueError(f"FASTMCP_SERVER_AUTH provider {provider_path} is not an AuthProvider")

    provider_config = _LEGACY_AUTH_PROVIDER_ENV.get(provider_path)
    kwargs: dict[str, Any] = {}
    if provider_config is not None:
        prefix, fields = provider_config
        for field_name in fields:
            env_name = f"FASTMCP_SERVER_AUTH_{prefix}{field_name.upper()}"
            value = _get_legacy_auth_env(env_name, auth_file_values)
            if value is None:
                continue
            kwargs[field_name] = _parse_auth_provider_env_value(
                provider_path,
                field_name,
                env_name,
                value,
            )

    scope_default = _LEGACY_AUTH_SCOPE_DEFAULTS.get(provider_path)
    if scope_default is not None and not kwargs.get("required_scopes"):
        kwargs["required_scopes"] = scope_default
    if provider_path in _LEGACY_ZERO_TIMEOUT_DEFAULTS and kwargs.get("timeout_seconds") == 0:
        kwargs["timeout_seconds"] = 10
    if (
        provider_path == "fastmcp.server.auth.providers.aws.AWSCognitoProvider"
        and kwargs.get("aws_region") == ""
    ):
        kwargs["aws_region"] = "eu-central-1"
    if provider_path == (
        "fastmcp.server.auth.providers.introspection.IntrospectionTokenVerifier"
    ):
        prefix = _LEGACY_AUTH_PROVIDER_ENV[provider_path][0]
        for required_field in ("introspection_url", "client_id", "client_secret"):
            if not kwargs.get(required_field):
                env_name = f"FASTMCP_SERVER_AUTH_{prefix}{required_field.upper()}"
                raise ValueError(
                    f"{env_name} is required for FastMCP auth provider {provider_path}"
                )

    if (
        provider_path == "fastmcp.server.auth.providers.supabase.SupabaseProvider"
        and str(kwargs.get("algorithm", "")).upper() == "HS256"
    ):
        raise ValueError(
            "FASTMCP_SERVER_AUTH_SUPABASE_ALGORITHM=HS256 is not supported by FastMCP 4. "
            "Configure RS256 or ES256."
        )

    try:
        provider = provider_class(**kwargs)
    except Exception as exc:
        raise ValueError(
            f"Invalid FASTMCP_SERVER_AUTH configuration for provider {provider_path} "
            f"({type(exc).__name__})"
        ) from None
    return provider


def _resolve_auth(mcp_config, transport: Optional[str] = None) -> Dict[str, Any]:
    """Resolve FastMCP auth kwargs for the requested transport.

    Returning {"auth": None} explicitly disables auth. FastMCP 4 removed
    environment provider loading, so this server retains the documented
    FastMCP 2 environment contract through a compatibility loader.
    """
    transport = transport or mcp_config.server_transport
    if transport not in _HTTP_TRANSPORTS:
        return {}

    auth_file_values = _get_legacy_auth_file_values()
    provider_path = _get_legacy_auth_env("FASTMCP_SERVER_AUTH", auth_file_values)

    configured = {
        "CLICKHOUSE_MCP_AUTH_DISABLED": mcp_config.auth_disabled,
        "CLICKHOUSE_MCP_AUTH_TOKEN": bool(mcp_config.auth_token),
        "FASTMCP_SERVER_AUTH": bool(provider_path),
    }
    active = [name for name, is_set in configured.items() if is_set]

    if len(active) > 1:
        raise ValueError(
            "Multiple authentication modes configured for HTTP/SSE transport: "
            f"{', '.join(active)}. These are mutually exclusive; unset all but one."
        )

    if not active:
        raise ValueError(
            "Authentication is required for HTTP/SSE transports. Configure exactly one of:\n"
            "  - CLICKHOUSE_MCP_AUTH_TOKEN=<token>   (static bearer token)\n"
            "  - FASTMCP_SERVER_AUTH=<class-path>    (FastMCP auth provider, full class path;\n"
            "       e.g. fastmcp.server.auth.providers.azure.AzureProvider)\n"
            "  - CLICKHOUSE_MCP_AUTH_DISABLED=true   (disables auth; development only)"
        )

    if mcp_config.auth_disabled:
        logger.warning("WARNING: MCP SERVER AUTHENTICATION IS DISABLED")
        logger.warning("Only use this for local development/testing.")
        logger.warning("DO NOT expose to networks.")
        return {"auth": None}

    if mcp_config.auth_token:
        verifier = StaticTokenVerifier(
            tokens={mcp_config.auth_token: {"client_id": "mcp-client", "scopes": []}},
            required_scopes=[],
        )
        logger.info("Authentication enabled for HTTP/SSE transport (static bearer token)")
        return {"auth": verifier}

    assert provider_path is not None
    logger.info("Authentication delegated to FastMCP provider: %s", provider_path)
    return {
        "auth": _load_fastmcp_auth_provider(
            provider_path,
            auth_file_values=auth_file_values,
        )
    }


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


mcp = ClickHouseFastMCP(
    name=MCP_SERVER_NAME,
    version=MCP_SERVER_VERSION,
    instructions=CLICKHOUSE_SERVER_INSTRUCTIONS,
)
_chdb_client = None
_chdb_error_message: Optional[str] = None


def _probe_clickhouse_health(config: dict) -> None:
    """Run an authenticated ClickHouse health query with a leased client."""
    entry = _acquire_clickhouse_client(config)
    try:
        entry.client.command("SELECT 1")
    finally:
        _release_client_entry(entry)


def _bounded_health_config(config: dict) -> dict:
    """Cap ClickHouse network timeouts to the public health timeout."""
    bounded = _ResolvedClientConfig(
        dict(config),
        overrides_applied=getattr(config, "overrides_applied", False),
    )
    for key in ("connect_timeout", "send_receive_timeout"):
        value = bounded.get(key)
        if value is None or value > _HEALTH_CHECK_TIMEOUT_SECONDS:
            bounded[key] = _HEALTH_CHECK_TIMEOUT_SECONDS
    return bounded


def _clear_completed_health_probe(future: concurrent.futures.Future) -> None:
    """Clear the shared health future when its probe finishes."""
    global _health_probe_future
    with _health_probe_lock:
        if _health_probe_future is future:
            _health_probe_future = None


def _get_health_probe_future(config: dict) -> concurrent.futures.Future:
    """Return the single in-flight ClickHouse health probe."""
    global _health_probe_future
    with _health_probe_lock:
        if _health_probe_future is not None and not _health_probe_future.done():
            return _health_probe_future
        future = HEALTH_EXECUTOR.submit(
            _probe_clickhouse_health,
            _bounded_health_config(config),
        )
        _health_probe_future = future
    future.add_done_callback(_clear_completed_health_probe)
    return future


def _claim_health_probe_log(future: Optional[concurrent.futures.Future]) -> bool:
    """Return true once for each shared health probe future."""
    if future is None:
        return True
    with _health_probe_lock:
        if future in _logged_health_probe_futures:
            return False
        _logged_health_probe_futures.add(future)
        return True


def _retrieve_health_probe_wrapper_result(future: asyncio.Future) -> None:
    """Retrieve a completed health wrapper result."""
    try:
        future.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    """Liveness probe. Intentionally unauthenticated and minimal.

    Debug via server logs.
    """
    future = None
    try:
        # Check if ClickHouse is enabled by trying to create config
        # If ClickHouse is disabled, this will succeed but connection will fail
        clickhouse_enabled = os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true"

        if not clickhouse_enabled:
            # If ClickHouse is disabled, check chDB status
            chdb_config = get_chdb_config()
            if chdb_config.enabled and _chdb_client is not None:
                return PlainTextResponse("OK")
            elif chdb_config.enabled and _chdb_error_message:
                return PlainTextResponse(
                    "ERROR. chDB initialization failed. Check server logs for details.",
                    status_code=503,
                )
            else:
                logger.error(
                    "Health check failed: both CLICKHOUSE_ENABLED=false and CHDB_ENABLED=false"
                )
                return PlainTextResponse(
                    "ERROR. Server misconfigured. Check server logs for details.",
                    status_code=503,
                )

        future = _get_health_probe_future(_resolve_client_config())
        wrapped_future = asyncio.wrap_future(future)
        wrapped_future.add_done_callback(_retrieve_health_probe_wrapper_result)
        await asyncio.wait_for(
            asyncio.shield(wrapped_future),
            timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
        )
        return PlainTextResponse("OK")
    except asyncio.TimeoutError:
        if _claim_health_probe_log(future):
            logger.warning(
                "Health check timed out after %.1f seconds",
                _HEALTH_CHECK_TIMEOUT_SECONDS,
            )
        return PlainTextResponse(
            "ERROR. ClickHouse connection failed. Check server logs for details.",
            status_code=503,
        )
    except Exception:
        # Log the underlying error server-side, but don't leak details over the wire.
        if _claim_health_probe_log(future):
            logger.exception("Health check failed: ClickHouse connection error")
        return PlainTextResponse(
            "ERROR. ClickHouse connection failed. Check server logs for details.",
            status_code=503,
        )


def result_to_table(query_columns, result) -> List[Table]:
    return [Table(**dict(zip(query_columns, row))) for row in result]


def result_to_column(query_columns, result) -> List[Column]:
    return [Column(**dict(zip(query_columns, row))) for row in result]


_JS_MAX_SAFE_INTEGER = 9007199254740991


def _stringify_unsafe_integers(
    obj: Any, active_container_ids: Optional[set[int]] = None
) -> Any:
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return str(int(obj)) if abs(obj) > _JS_MAX_SAFE_INTEGER else obj
    if not isinstance(obj, (dict, list, tuple)):
        return obj

    if active_container_ids is None:
        active_container_ids = set()
    container_id = id(obj)
    if container_id in active_container_ids:
        return obj

    active_container_ids.add(container_id)
    try:
        if isinstance(obj, dict):
            items = iter(obj.items())
            for key, value in items:
                converted = _stringify_unsafe_integers(value, active_container_ids)
                if converted is value:
                    continue
                result = dict(obj)
                result[key] = converted
                for remaining_key, remaining_value in items:
                    result[remaining_key] = _stringify_unsafe_integers(
                        remaining_value, active_container_ids
                    )
                return result
            return obj

        items = enumerate(obj)
        for index, value in items:
            converted = _stringify_unsafe_integers(value, active_container_ids)
            if converted is value:
                continue
            result = list(obj)
            result[index] = converted
            for remaining_index, remaining_value in items:
                result[remaining_index] = _stringify_unsafe_integers(
                    remaining_value, active_container_ids
                )
            return result
        return obj
    finally:
        active_container_ids.remove(container_id)


def _serialize_tool_result_with_simplejson(obj: Any) -> str:
    return simplejson.dumps(
        obj,
        default=str,
        bigint_as_string=True,
        allow_nan=True,
        use_decimal=False,
        namedtuple_as_object=False,
        encoding=None,
    )


def _serialize_tool_result_with_stdlib(obj: Any) -> str:
    return json.dumps(_stringify_unsafe_integers(obj), default=str)


# The pure Python simplejson encoder is slower than the stdlib fallback.
if getattr(simplejson.encoder, "c_make_encoder", None) is not None:
    _serialize_tool_result = _serialize_tool_result_with_simplejson
else:
    _serialize_tool_result = _serialize_tool_result_with_stdlib


def list_databases() -> str:
    """List available ClickHouse databases"""
    return _list_databases_with_config(_resolve_client_config())


def _list_databases_with_config(config: dict[str, Any]) -> str:
    """List databases with a resolved client configuration."""
    logger.info("Listing all databases")

    for attempt in range(2):
        entry = None
        try:
            entry = _acquire_clickhouse_client(config)
            client = entry.client
            result = client.command("SHOW DATABASES")
            break
        except Exception as err:
            if attempt == 0 and _is_connection_error(err):
                logger.warning("list_databases connection error, retrying: %s", err)
                if entry is not None:
                    _evict_cached_client(config, entry.client)
                continue
            raise
        finally:
            if entry is not None:
                _release_client_entry(entry)

    # Convert newline-separated string to list and trim whitespace
    if isinstance(result, str):
        databases = [db.strip() for db in result.strip().split("\n")]
    else:
        databases = [result]

    logger.info(f"Found {len(databases)} databases")
    return _serialize_tool_result(databases)


@wraps(list_databases)
async def list_databases_async() -> str:
    overrides = await _get_client_config_overrides_for_tool()
    config = _resolve_client_config(overrides)
    future = METADATA_EXECUTOR.submit(_list_databases_with_config, config)
    return await asyncio.wrap_future(future)


# Store pagination state for list_tables with an absolute 1-hour expiry.
table_pagination_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)  # 3600 seconds = 1 hour
_table_pagination_cache_lock = threading.Lock()
_PAGE_TOKEN_EXPIRES_AT = "_expires_at"
_CLAIM_PAGE_TOKEN_IN_WORKER = object()


@dataclass(frozen=True)
class _PendingPageToken:
    token: str
    state: dict[str, Any]


@dataclass(frozen=True)
class _PreparedListTablesResult:
    response: str
    pending_page_token: Optional[_PendingPageToken]


def fetch_table_names_from_system(
    client,
    database: str,
    like: Optional[str] = None,
    not_like: Optional[str] = None,
) -> List[str]:
    """Get list of table names from system.tables.

    Args:
        client: ClickHouse client
        database: Database name
        like: Optional pattern to filter table names (LIKE)
        not_like: Optional pattern to filter out table names (NOT LIKE)

    Returns:
        List of table names
    """
    query = f"SELECT name FROM system.tables WHERE database = {format_query_value(database)}"
    if like:
        query += f" AND name LIKE {format_query_value(like)}"

    if not_like:
        query += f" AND name NOT LIKE {format_query_value(not_like)}"

    result = client.query(query)
    table_names = [row[0] for row in result.result_rows]
    return table_names


def get_paginated_table_data(
    client,
    database: str,
    table_names: List[str],
    start_idx: int,
    page_size: int,
    include_detailed_columns: bool = True,
) -> tuple[List[Table], int, bool]:
    """Get detailed information for a page of tables.

    Args:
        client: ClickHouse client
        database: Database name
        table_names: List of all table names to paginate
        start_idx: Starting index for pagination
        page_size: Number of tables per page
        include_detailed_columns: Whether to include detailed column metadata (default: True)

    Returns:
        Tuple of (list of Table objects, end index, has more pages)
    """
    end_idx = min(start_idx + page_size, len(table_names))
    current_page_table_names = table_names[start_idx:end_idx]

    if not current_page_table_names:
        return [], end_idx, False

    query = f"""
        SELECT database, name, engine, create_table_query, dependencies_database,
               dependencies_table, engine_full, sorting_key, primary_key, total_rows,
               total_bytes, total_bytes_uncompressed, parts, active_parts, total_marks, comment
        FROM system.tables
        WHERE database = {format_query_value(database)}
        AND name IN ({", ".join(format_query_value(name) for name in current_page_table_names)})
    """

    result = client.query(query)
    tables = result_to_table(result.column_names, result.result_rows)

    if include_detailed_columns:
        for table in tables:
            column_data_query = f"""
                SELECT database, table, name, type AS column_type, default_kind, default_expression, comment
                FROM system.columns
                WHERE database = {format_query_value(database)}
                AND table = {format_query_value(table.name)}
            """
            column_data_query_result = client.query(column_data_query)
            table.columns = result_to_column(
                column_data_query_result.column_names,
                column_data_query_result.result_rows,
            )
    else:
        for table in tables:
            table.columns = []

    return tables, end_idx, end_idx < len(table_names)


def create_page_token(
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    table_names: List[str],
    end_idx: int,
    include_detailed_columns: bool,
) -> str:
    """Create a new page token and store it in the cache.

    Args:
        database: Database name
        like: LIKE pattern used to filter tables
        not_like: NOT LIKE pattern used to filter tables
        table_names: List of all table names
        end_idx: Index to start from for the next page
        include_detailed_columns: Whether to include detailed column metadata

    Returns:
        New page token
    """
    pending_page_token = _prepare_page_token(
        database,
        like,
        not_like,
        table_names,
        end_idx,
        include_detailed_columns,
    )
    _commit_page_token(pending_page_token)
    return pending_page_token.token


def _prepare_page_token(
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    table_names: List[str],
    end_idx: int,
    include_detailed_columns: bool,
) -> _PendingPageToken:
    """Prepare a page token without making it available to clients."""
    return _PendingPageToken(
        token=str(uuid.uuid4()),
        state={
            "database": database,
            "like": like,
            "not_like": not_like,
            "table_names": table_names,
            "start_idx": end_idx,
            "include_detailed_columns": include_detailed_columns,
        },
    )


def _commit_page_token(pending_page_token: _PendingPageToken) -> None:
    """Make a prepared page token available for one hour."""
    with _table_pagination_cache_lock:
        expires_at = table_pagination_cache.timer() + table_pagination_cache.ttl
        state = dict(pending_page_token.state)
        state[_PAGE_TOKEN_EXPIRES_AT] = expires_at
        table_pagination_cache[pending_page_token.token] = state


def _claim_page_token_for_request(
    page_token: str,
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    include_detailed_columns: bool,
) -> Optional[dict[str, Any]]:
    """Claim a matching page token and leave a mismatched token untouched."""
    mismatch = False
    with _table_pagination_cache_lock:
        state = table_pagination_cache.get(page_token)
        if state is None:
            return None
        expires_at = state.get(_PAGE_TOKEN_EXPIRES_AT)
        if expires_at is not None and expires_at <= table_pagination_cache.timer():
            table_pagination_cache.pop(page_token, None)
            return None
        cached_include_detailed = state.get("include_detailed_columns", True)
        mismatch = (
            state["database"] != database
            or state["like"] != like
            or state["not_like"] != not_like
            or cached_include_detailed != include_detailed_columns
        )
        if not mismatch:
            return table_pagination_cache.pop(page_token)

    logger.warning(
        "Page token %s is for a different database, filter, or metadata setting. "
        "Ignoring token and starting from beginning.",
        page_token,
    )
    return None


def _restore_page_token(page_token: str, state: dict[str, Any]) -> None:
    """Restore a claimed pagination token unless another value already exists."""
    with _table_pagination_cache_lock:
        expires_at = state.get(_PAGE_TOKEN_EXPIRES_AT)
        if expires_at is not None and expires_at <= table_pagination_cache.timer():
            return
        table_pagination_cache.setdefault(page_token, state)


def list_tables(
    database: str,
    like: Optional[str] = None,
    not_like: Optional[str] = None,
    page_token: Optional[str] = None,
    page_size: Annotated[int, Field(gt=0)] = 50,
    include_detailed_columns: bool = True,
) -> str:
    """List available ClickHouse tables in a database, including schema, comment,
    row count, and column count.

    Integers outside [-9007199254740991, 9007199254740991] in table metadata are
    returned as decimal strings.
    Pagination tokens are single-use and retained for up to one hour.

    Args:
        database: The database to list tables from
        like: Optional LIKE pattern to filter table names
        not_like: Optional NOT LIKE pattern to exclude table names
        page_token: Single-use token from a previous call, retained for up to one hour
        page_size: Number of tables to return per page (default: 50, must be greater than 0)
        include_detailed_columns: Whether to include detailed column metadata (default: True).
            When False, the columns array will be empty but create_table_query still contains
            all column information. This reduces payload size for large schemas.

    Returns:
        A JSON-encoded string of an object containing:
        - tables: List of table information (as dictionaries)
        - next_page_token: Token for the next page, or None if no more pages
        - total_tables: Total number of tables matching the filters
    """
    if page_size <= 0:
        raise ToolError("page_size must be greater than 0")

    return _list_tables_with_config(
        _resolve_client_config(),
        database,
        like,
        not_like,
        page_token,
        page_size,
        include_detailed_columns,
    )


def _list_tables_with_config(
    config: dict[str, Any],
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    page_token: Optional[str],
    page_size: int,
    include_detailed_columns: bool,
    claimed_page_state: object = _CLAIM_PAGE_TOKEN_IN_WORKER,
) -> str | _PreparedListTablesResult:
    """List tables with a resolved client configuration."""
    if page_size <= 0:
        raise ToolError("page_size must be greater than 0")

    owns_page_token_transaction = claimed_page_state is _CLAIM_PAGE_TOKEN_IN_WORKER
    if owns_page_token_transaction:
        claimed_page_state = (
            _claim_page_token_for_request(
                page_token,
                database,
                like,
                not_like,
                include_detailed_columns,
            )
            if page_token
            else None
        )

    logger.info(
        "Listing tables in database '%s' with like=%s, not_like=%s, "
        "page_token=%s, page_size=%s, include_detailed_columns=%s",
        database,
        like,
        not_like,
        page_token,
        page_size,
        include_detailed_columns,
    )

    try:
        for attempt in range(2):
            entry = None
            try:
                entry = _acquire_clickhouse_client(config)
                client = entry.client
                prepared_result = _list_tables_impl(
                    client,
                    database,
                    like,
                    not_like,
                    page_token,
                    page_size,
                    include_detailed_columns,
                    claimed_page_state,
                )
                break
            except Exception as err:
                if attempt == 0 and _is_connection_error(err):
                    logger.warning("list_tables connection error, retrying: %s", err)
                    if entry is not None:
                        _evict_cached_client(config, entry.client)
                    continue
                raise
            finally:
                if entry is not None:
                    _release_client_entry(entry)
    except BaseException:
        if owns_page_token_transaction and page_token and claimed_page_state is not None:
            _restore_page_token(page_token, claimed_page_state)
        raise

    try:
        if not owns_page_token_transaction:
            return prepared_result
        if prepared_result.pending_page_token is not None:
            _commit_page_token(prepared_result.pending_page_token)
        return prepared_result.response
    except BaseException:
        if owns_page_token_transaction and page_token and claimed_page_state is not None:
            _restore_page_token(page_token, claimed_page_state)
        raise


@wraps(list_tables)
async def list_tables_async(
    database: str,
    like: Optional[str] = None,
    not_like: Optional[str] = None,
    page_token: Optional[str] = None,
    page_size: Annotated[int, Field(gt=0)] = 50,
    include_detailed_columns: bool = True,
) -> str:
    overrides = await _get_client_config_overrides_for_tool()
    config = _resolve_client_config(overrides)
    claimed_page_state = (
        _claim_page_token_for_request(
            page_token,
            database,
            like,
            not_like,
            include_detailed_columns,
        )
        if page_token
        else None
    )
    completed = False
    try:
        future = METADATA_EXECUTOR.submit(
            _list_tables_with_config,
            config,
            database,
            like,
            not_like,
            page_token,
            page_size,
            include_detailed_columns,
            claimed_page_state,
        )
        prepared_result = await asyncio.wrap_future(future)
        if isinstance(prepared_result, str):
            completed = True
            return prepared_result
        if prepared_result.pending_page_token is not None:
            _commit_page_token(prepared_result.pending_page_token)
        completed = True
        return prepared_result.response
    finally:
        if not completed and page_token and claimed_page_state is not None:
            _restore_page_token(page_token, claimed_page_state)


def _list_tables_impl(
    client,
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    page_token: Optional[str],
    page_size: int,
    include_detailed_columns: bool,
    claimed_page_state: object = None,
) -> _PreparedListTablesResult:
    """Inner implementation of list_tables, separated for retry logic."""
    if claimed_page_state is not None:
        table_names = claimed_page_state["table_names"]
        start_idx = claimed_page_state["start_idx"]

        tables, end_idx, has_more = get_paginated_table_data(
            client,
            database,
            table_names,
            start_idx,
            page_size,
            include_detailed_columns,
        )

        pending_page_token = None
        if has_more:
            pending_page_token = _prepare_page_token(
                database, like, not_like, table_names, end_idx, include_detailed_columns
            )
        next_page_token = pending_page_token.token if pending_page_token else None

        logger.info(
            "Returned page with %s tables (total: %s), next_page_token=%s",
            len(tables),
            len(table_names),
            next_page_token,
        )
        return _PreparedListTablesResult(
            response=_serialize_tool_result({
                "tables": [asdict(table) for table in tables],
                "next_page_token": next_page_token,
                "total_tables": len(table_names),
            }),
            pending_page_token=pending_page_token,
        )

    table_names = fetch_table_names_from_system(client, database, like, not_like)

    start_idx = 0
    tables, end_idx, has_more = get_paginated_table_data(
        client,
        database,
        table_names,
        start_idx,
        page_size,
        include_detailed_columns,
    )

    pending_page_token = None
    if has_more:
        pending_page_token = _prepare_page_token(
            database, like, not_like, table_names, end_idx, include_detailed_columns
        )
    next_page_token = pending_page_token.token if pending_page_token else None

    logger.info(
        "Found %s tables, returning %s with next_page_token=%s",
        len(table_names),
        len(tables),
        next_page_token,
    )

    return _PreparedListTablesResult(
        response=_serialize_tool_result({
            "tables": [asdict(table) for table in tables],
            "next_page_token": next_page_token,
            "total_tables": len(table_names),
        }),
        pending_page_token=pending_page_token,
    )


# SQL comments and quoted text, blanked out before destructive-keyword matching.
# A keyword inside a string literal must not trigger the guard, and a keyword
# placed after a comment marker must not slip past it.
_SQL_COMMENTS_AND_QUOTED_TEXT = re.compile(
    r"""
      '(?:\\.|''|[^'\\])*'              # string literal
    | "(?:\\.|""|[^"\\])*"              # double-quoted identifier
    | `(?:\\.|``|[^`\\])*`              # backtick-quoted identifier
    | \$(?P<tag>\w*)\$.*?\$(?P=tag)\$    # dollar-quoted string or heredoc
    | --[^\n]*                         # line comment
    | \#[^\n]*                         # line comment
    | /\*.*?\*/                        # block comment
    """,
    re.VERBOSE | re.DOTALL,
)

# Matched against the scrubbed statement. Bare DROP also covers the
# ALTER ... DROP PARTITION/PART/COLUMN clauses. TRUNCATE followed by an open
# parenthesis is the rounding function, as is replace() after OR. Bare DELETE
# and UPDATE cover both the lightweight and ALTER mutation forms. REPLACE
# TABLE/PARTITION and OR REPLACE overwrite existing data. Bare CLEAR is
# reversible, so only CLEAR COLUMN/INDEX/PROJECTION is flagged.
_DESTRUCTIVE_KEYWORDS = re.compile(
    r"""
      \bDROP\b
    | \bTRUNCATE\b(?!\s*\()
    | \bDELETE\b
    | \bUPDATE\b
    | \bREPLACE\s+(?:TABLE|PARTITION)\b
    | \bOR\s+REPLACE\b(?!\s*\()
    | \bCLEAR\s+(?:COLUMN|INDEX|PROJECTION)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# DETACH ... PERMANENTLY is matched as two independent searches. A single
# `DETACH .* PERMANENTLY` branch backtracks quadratically on crafted input,
# and the validator runs on an executor thread that cancel cannot stop.
# Plain DETACH is reversible via ATTACH and stays allowed.
_DETACH_KEYWORD = re.compile(r"\bDETACH\b", re.IGNORECASE)
_PERMANENTLY_KEYWORD = re.compile(r"\bPERMANENTLY\b", re.IGNORECASE)


def _strip_comments_and_quoted_text(query: str) -> str:
    """Blank out comments and quoted text so keyword matching sees only SQL syntax.

    Each match is replaced by a single space to keep surrounding tokens separate.
    Unterminated literals and comments do not match and are left in place, which
    keeps the destructive-operation check on the conservative side.
    """
    return _SQL_COMMENTS_AND_QUOTED_TEXT.sub(" ", query)


def _validate_query_for_destructive_ops(query: str) -> None:
    """Reject destructive statements unless CLICKHOUSE_ALLOW_DROP is set.

    Args:
        query: The SQL query to validate

    Raises:
        ToolError: If the query contains a destructive statement and CLICKHOUSE_ALLOW_DROP is not set
    """
    config = get_config()

    # If writes are not enabled, skip this check (readonly mode will catch it anyway)
    if not config.allow_write_access:
        return

    # If DROP is explicitly allowed, no validation needed
    if config.allow_drop:
        return

    statement = _strip_comments_and_quoted_text(query)
    if _DESTRUCTIVE_KEYWORDS.search(statement) or (
        _DETACH_KEYWORD.search(statement) and _PERMANENTLY_KEYWORD.search(statement)
    ):
        raise ToolError(
            "Destructive operations are not allowed (DROP, TRUNCATE, DELETE, UPDATE, "
            "REPLACE TABLE/PARTITION, CREATE OR REPLACE, CLEAR COLUMN/INDEX/PROJECTION, "
            "DETACH PERMANENTLY). Set CLICKHOUSE_ALLOW_DROP=true to enable them. "
            "This gate is a best-effort accident guard, not a security boundary. "
            "Restrict the ClickHouse user's grants for real enforcement."
        )


def _is_connection_error(err: Exception) -> bool:
    """Check if an exception indicates a broken connection rather than a query error."""
    if isinstance(err, (OSError, ConnectionError, OperationalError)):
        return True
    err_str = str(err).lower()
    return any(s in err_str for s in ("connection", "timed out", "reset by peer", "eof"))


def _register_active_query(query_id: str, query: str) -> _ActiveQueryState:
    """Register query state before its worker is submitted."""
    state = _ActiveQueryState(query=query)
    with _active_queries_lock:
        _active_queries[query_id] = state
    return state


def _remove_active_query(query_id: str, state: _ActiveQueryState) -> None:
    """Remove query state if it still belongs to this execution."""
    with _active_queries_lock:
        if _active_queries.get(query_id) is state:
            _active_queries.pop(query_id)


def _mark_active_query_cancelled(query_id: str) -> Optional[_ActiveQueryState]:
    """Mark an active query cancelled before any server-side KILL attempt."""
    with _active_queries_lock:
        state = _active_queries.get(query_id)
        if state is not None:
            state.cancelled = True
        return state


def execute_query(query: str, query_id: str, client_config: dict) -> str:
    """Execute a query in a worker thread with a pre-resolved client config."""
    with _active_queries_lock:
        state = _active_queries.get(query_id)
        if state is None:
            state = _ActiveQueryState(query=query)
            _active_queries[query_id] = state

    entry = None
    try:
        entry = _acquire_clickhouse_client(client_config)
        client = entry.client
        with _active_queries_lock:
            if state.cancelled:
                raise ToolError("Query cancelled before execution")
            state.client_entry = entry

        _validate_query_for_destructive_ops(query)

        query_settings = build_query_settings(client)
        query_settings["query_id"] = query_id
        with _active_queries_lock:
            if state.cancelled:
                raise ToolError("Query cancelled before execution")
        res = client.query(query, settings=query_settings)
        logger.info(f"Query {query_id} returned {len(res.result_rows)} rows")
        return _serialize_tool_result({"columns": res.column_names, "rows": res.result_rows})
    except ToolError:
        raise
    except Exception as err:
        # Do not retry queries because a write may already have succeeded.
        if entry is not None and _is_connection_error(err):
            _evict_cached_client(client_config, client)
        logger.error(f"Error executing query {query_id}: {err}")
        raise ToolError(f"Query execution failed: {str(err)}")
    finally:
        _remove_active_query(query_id, state)
        if entry is not None:
            _release_client_entry(entry)


def _cancel_query(query_id: str):
    """Issue KILL QUERY on the ClickHouse server for a timed-out query.

    Uses the same cached client that originated the query. Cancellation
    failures are logged without masking the original timeout.
    """
    state = _mark_active_query_cancelled(query_id)

    if state is None:
        logger.debug("Query %s already completed, nothing to cancel", query_id)
        return

    try:
        safe_id = str(uuid.UUID(query_id))
    except ValueError:
        logger.warning("Refusing to KILL QUERY with non-UUID query_id: %r", query_id)
        return

    client = None
    try:
        with _client_cache_lock:
            client_entry = state.client_entry
            if client_entry is None or client_entry.closed:
                client = None
            else:
                client_entry.active_users += 1
                client = client_entry.client
        if client is None:
            logger.warning(
                "Query %s cancelled before client acquisition completed",
                safe_id,
            )
            return

        logger.info("Cancelling query %s via KILL QUERY", safe_id)
        client.command(
            f"KILL QUERY WHERE query_id = {format_query_value(safe_id)}"
        )
        logger.info("Successfully cancelled query %s", safe_id)
    except Exception as e:
        logger.warning("Failed to cancel query %s: %s", safe_id, e)
    finally:
        if client is not None:
            _release_client_entry(client_entry)


def _cancel_query_with_bounded_wait(query_id: str) -> None:
    """Run cancellation in its executor and wait briefly for completion."""
    future = CANCELLATION_EXECUTOR.submit(_cancel_query, query_id)
    try:
        future.result(timeout=_QUERY_CANCELLATION_WAIT_SECONDS)
    except concurrent.futures.TimeoutError:
        logger.warning(
            "Cancellation for query %s exceeded %.1f seconds",
            query_id,
            _QUERY_CANCELLATION_WAIT_SECONDS,
        )


async def _cancel_query_async(query_id: str) -> None:
    """Await cancellation briefly without blocking the event loop."""
    future = CANCELLATION_EXECUTOR.submit(_cancel_query, query_id)
    try:
        await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(future)),
            timeout=_QUERY_CANCELLATION_WAIT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Cancellation for query %s exceeded %.1f seconds",
            query_id,
            _QUERY_CANCELLATION_WAIT_SECONDS,
        )


def run_query(query: str) -> str:
    """Execute a SQL query against ClickHouse.

    Queries run in read-only mode by default. Set CLICKHOUSE_ALLOW_WRITE_ACCESS=true
    to allow DDL and DML statements when your ClickHouse server permits them.
    """
    logger.info(f"Executing query: {query}")

    client_config = _resolve_client_config()
    query_id = str(uuid.uuid4())
    state = _register_active_query(query_id, query)

    try:
        with _active_queries_lock:
            in_flight = len(_active_queries)
        if in_flight >= _max_workers:
            logger.warning(
                "Thread pool saturated: %d in-flight vs %d workers",
                in_flight, _max_workers,
            )

        try:
            future = QUERY_EXECUTOR.submit(execute_query, query, query_id, client_config)
        except Exception:
            _remove_active_query(query_id, state)
            raise
        timeout_secs = get_mcp_config().query_timeout
        try:
            return future.result(timeout=timeout_secs)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "Query %s timed out after %s seconds: %s", query_id, timeout_secs, query
            )
            if future.cancel():
                _remove_active_query(query_id, state)
            else:
                _mark_active_query_cancelled(query_id)
                _cancel_query_with_bounded_wait(query_id)
            raise ToolError(f"Query timed out after {timeout_secs} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error("Unexpected error in run_query: %s", str(e))
        raise RuntimeError(f"Unexpected error during query execution: {str(e)}")


def _freeze_client_config_value(value: Any) -> Optional[tuple]:
    """Convert a client config value into a stable cache-key component."""
    if isinstance(value, Mapping):
        frozen_items = []
        try:
            items = sorted(value.items())
        except TypeError:
            return None
        for key, nested_value in items:
            frozen_value = _freeze_client_config_value(nested_value)
            if frozen_value is None:
                return None
            frozen_items.append((key, frozen_value))
        return ("mapping", tuple(frozen_items))
    if isinstance(value, list):
        frozen_items = tuple(_freeze_client_config_value(item) for item in value)
        if any(item is None for item in frozen_items):
            return None
        return ("list", frozen_items)
    if isinstance(value, tuple):
        frozen_items = tuple(_freeze_client_config_value(item) for item in value)
        if any(item is None for item in frozen_items):
            return None
        return ("tuple", frozen_items)
    if isinstance(value, (set, frozenset)):
        frozen_items = tuple(_freeze_client_config_value(item) for item in value)
        if any(item is None for item in frozen_items):
            return None
        return ("set", frozenset(frozen_items))
    try:
        hash(value)
    except TypeError:
        return None
    return ("value", value)


def _config_to_cache_key(config: dict) -> Optional[tuple]:
    """Convert a client config dict into a stable cache key when possible."""
    frozen_config = []
    for key, value in sorted(config.items()):
        frozen_value = _freeze_client_config_value(value)
        if frozen_value is None:
            return None
        frozen_config.append((key, frozen_value))
    return tuple(frozen_config)


async def run_query_async(query: str) -> str:
    """Async MCP-facing wrapper for ClickHouse queries.

    Awaits the worker-pool future asynchronously so concurrent tool calls are
    served while a slow query is in flight.
    """
    logger.info(f"Executing query: {query}")

    overrides = await _get_client_config_overrides_for_tool()
    client_config = _resolve_client_config(overrides)
    query_id = str(uuid.uuid4())
    state = _register_active_query(query_id, query)

    try:
        with _active_queries_lock:
            in_flight = len(_active_queries)
        if in_flight >= _max_workers:
            logger.warning(
                "Thread pool saturated: %d in-flight vs %d workers",
                in_flight, _max_workers,
            )

        try:
            future = QUERY_EXECUTOR.submit(execute_query, query, query_id, client_config)
        except Exception:
            _remove_active_query(query_id, state)
            raise
        timeout_secs = get_mcp_config().query_timeout
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=timeout_secs
            )
        except asyncio.CancelledError:
            if future.cancel():
                _remove_active_query(query_id, state)
            else:
                _mark_active_query_cancelled(query_id)
                await _cancel_query_async(query_id)
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "Query %s timed out after %s seconds: %s", query_id, timeout_secs, query
            )
            if future.cancel():
                _remove_active_query(query_id, state)
            else:
                _mark_active_query_cancelled(query_id)
                await _cancel_query_async(query_id)
            raise ToolError(f"Query timed out after {timeout_secs} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error("Unexpected error in run_query_async: %s", str(e))
        raise RuntimeError(f"Unexpected error during query execution: {str(e)}")


# ClickHouse native TCP protocol ports (clickhouse-client). This MCP server uses the
# HTTP interface only (default 8123 / 8443). Connecting to native ports fails with
# messages like "Port 9000 is for clickhouse-client program".
_NATIVE_PROTOCOL_PORTS = frozenset({9000, 9440})


def _connection_error_hints(error: Exception, client_config: dict) -> List[str]:
    """Return actionable hints for common ClickHouse connection misconfigurations.

    Helps users who confuse MCP transport settings with database settings, or who
    point CLICKHOUSE_PORT at the native TCP protocol instead of the HTTP interface.
    """
    hints: List[str] = []
    err = str(error).lower()
    port = client_config.get("port")
    secure = bool(client_config.get("secure"))
    host = client_config.get("host", "<unknown>")

    native_response_port = next(
        (
            native_port
            for native_port in _NATIVE_PROTOCOL_PORTS
            if f"port {native_port} is for clickhouse-client" in err
        ),
        None,
    )
    if port in _NATIVE_PROTOCOL_PORTS:
        hints.append(
            f"CLICKHOUSE_PORT={port} looks like ClickHouse's native TCP protocol port "
            "(used by clickhouse-client). This server uses the HTTP interface — set "
            "CLICKHOUSE_PORT to 8123 (HTTP) or 8443 (HTTPS), or your deployment's HTTP "
            "mapping. Do not use native ports 9000/9440."
        )
    elif native_response_port is not None:
        hints.append(
            f"The ClickHouse response indicates that this request reached native TCP port "
            f"{native_response_port}, even though the client was configured for {host}:{port}. "
            "Check DNS, service, proxy, load-balancer, and port mappings to ensure traffic is "
            "routed to ClickHouse's HTTP interface (8123/8443 by default, or your deployment's "
            "HTTP mapping)."
        )

    tls_tokens = (
        "ssl",
        "tls",
        "certificate",
        "handshake",
        "wrong version number",
        "certificate verify failed",
        "unexpected_eof",
        "eof occurred in violation of protocol",
    )
    if any(token in err for token in tls_tokens):
        scheme = "HTTPS" if secure else "HTTP"
        hints.append(
            f"TLS/SSL error while connecting with CLICKHOUSE_SECURE="
            f"{str(secure).lower()} ({scheme} to {host}:{port}). "
            "CLICKHOUSE_SECURE enables HTTPS for the ClickHouse database connection "
            "only — it is not MCP or ingress TLS. Use true for HTTPS database "
            "endpoints (ClickHouse Cloud / port 8443) and false only for plain HTTP "
            "(typical local Docker on 8123)."
        )

    # General connectivity and scheme/port failures can surface as opaque HTTP errors.
    connection_failure_tokens = (
        "http status",
        "bad status line",
        "connection refused",
        "connection reset",
        "remote end closed connection",
    )
    if any(token in err for token in connection_failure_tokens) and not hints:
        hints.append(
            f"Connection to {host}:{port} failed. Verify ClickHouse is running and reachable "
            "at this address and that network or proxy routing permits access. Then confirm "
            f"CLICKHOUSE_SECURE={str(secure).lower()} matches whether ClickHouse expects HTTPS, "
            "and that CLICKHOUSE_PORT is an HTTP interface port (8123/8443), not a native TCP "
            "port (9000/9440). These settings configure the database client, not the MCP "
            "server transport."
        )

    return hints


def _format_connection_failure(error: Exception, client_config: dict) -> str:
    """Build a connection failure message with optional configuration hints."""
    message = f"Failed to connect to ClickHouse: {error}"
    hints = _connection_error_hints(error, client_config)
    if hints:
        message += "\n" + "\n".join(f"Hint: {hint}" for hint in hints)
    return message


# Privileges the drop gate pretends to block but cannot enforce server-side.
# ALTER includes ALTER DELETE and ALTER DROP PARTITION in the privilege
# hierarchy. ALTER ADD is exempt because the README recipe grants it.
_GRANTS_ADVISORY_PRIVILEGES = re.compile(
    r"\b(ALL|DROP|TRUNCATE|DELETE|UPDATE|ALTER\b(?!\s+ADD\b))\b", re.IGNORECASE
)

_grants_advisory_done = False


def _warn_if_overprivileged(client) -> None:
    """Warn once when the drop gate is active but the ClickHouse user holds
    privileges it cannot enforce against. Fail-open, never raises.
    """
    global _grants_advisory_done
    if _grants_advisory_done:
        return
    # Check-then-set race across executor threads is harmless, worst case is a
    # duplicate warning.
    _grants_advisory_done = True

    try:
        result = client.query("SHOW GRANTS")
        matched: set[str] = set()
        role_grants = []
        for row in result.result_rows:
            grant = str(row[0])
            if grant.upper().startswith("GRANT") and not re.search(r"\bON\b", grant, re.IGNORECASE):
                # `GRANT <role> TO ...`; role privileges are not expanded here.
                role_grants.append(grant)
                continue
            matched.update(m.group(1).upper() for m in _GRANTS_ADVISORY_PRIVILEGES.finditer(grant))
        if matched:
            logger.warning(
                "CLICKHOUSE_ALLOW_DROP=false, but the ClickHouse user holds %s privileges. "
                "The destructive-operation gate runs in the MCP server and is not enforced "
                "server-side. See the README least-privilege recipe to restrict grants.",
                ", ".join(sorted(matched)),
            )
        for grant in role_grants:
            logger.info(
                "Grants advisory cannot inspect privileges granted via roles: %s", grant
            )
    except Exception as e:
        logger.debug("Grants advisory skipped: %s", e)


def _snapshot_client_config_overrides(overrides: Any) -> Optional[dict[str, Any]]:
    """Validate and copy request-scoped ClickHouse client overrides."""
    if overrides is None:
        return None
    if not isinstance(overrides, dict):
        raise ToolError(f"{CLIENT_CONFIG_OVERRIDES_KEY} must be a dict")

    snapshot = dict(overrides)
    for key in _REJECTED_ROLE_OVERRIDE_KEYS:
        if key in snapshot:
            raise ToolError(
                f"{CLIENT_CONFIG_OVERRIDES_KEY}.{key} is not supported; "
                f"use {CLIENT_CONFIG_OVERRIDES_KEY}.settings.role"
            )
    for key in _NESTED_CLIENT_CONFIG_KEYS:
        if key not in snapshot:
            continue
        value = snapshot[key]
        if not isinstance(value, Mapping):
            raise ToolError(f"{CLIENT_CONFIG_OVERRIDES_KEY}.{key} must be a mapping")
        if key == "generic_args":
            for role_key in _REJECTED_ROLE_OVERRIDE_KEYS:
                if role_key in value:
                    raise ToolError(
                        f"{CLIENT_CONFIG_OVERRIDES_KEY}.generic_args.{role_key} "
                        "is not supported; "
                        f"use {CLIENT_CONFIG_OVERRIDES_KEY}.settings.role"
                    )
        snapshot[key] = dict(value)
    return snapshot


def _request_client_config_overrides(ctx: Any) -> Optional[dict[str, Any]]:
    """Read request-local ClickHouse overrides from a FastMCP context."""
    request_state = getattr(ctx, "_request_state", None)
    make_state_key = getattr(ctx, "_make_state_key", None)
    if not isinstance(request_state, Mapping) or not callable(make_state_key):
        raise RuntimeError(
            "FastMCP request-local state API is unavailable; validate the FastMCP version"
        )
    overrides = request_state.get(make_state_key(CLIENT_CONFIG_OVERRIDES_KEY))
    return _snapshot_client_config_overrides(overrides)


def _get_client_config_overrides() -> Optional[dict[str, Any]]:
    """Capture ClickHouse client overrides from the active FastMCP request."""
    try:
        ctx = get_context()
    except RuntimeError:
        return None
    # FastMCP 4.0 get_state() falls back to session state. There is no public
    # request-only accessor. Validate this private adapter before a minor upgrade.
    return _request_client_config_overrides(ctx)


async def _get_client_config_overrides_for_tool() -> Optional[dict[str, Any]]:
    """Reject session-scoped overrides before an MCP-facing tool dispatch."""
    try:
        ctx = get_context()
    except RuntimeError:
        return None
    overrides = _request_client_config_overrides(ctx)
    if overrides is not None:
        return overrides
    session_value = await ctx.get_state(CLIENT_CONFIG_OVERRIDES_KEY)
    if session_value is not None:
        raise ToolError(
            f"{CLIENT_CONFIG_OVERRIDES_KEY} must be request-scoped; middleware must call "
            "Context.set_state(..., serializable=False)"
        )
    return None


def _apply_client_config_overrides(
    client_config: dict[str, Any], overrides: Optional[dict[str, Any]]
) -> None:
    """Merge request-scoped overrides into the base client configuration."""
    if overrides is None:
        return

    logger.debug(
        "Applying request-specific ClickHouse client config override keys: %s",
        list(overrides.keys()),
    )
    remaining_overrides = dict(overrides)
    for key in _NESTED_CLIENT_CONFIG_KEYS:
        if key not in remaining_overrides:
            continue
        base_value = client_config.get(key, {})
        if base_value is None:
            base_value = {}
        if not isinstance(base_value, Mapping):
            raise ToolError(f"Base ClickHouse client config {key} must be a mapping")
        client_config[key] = {**base_value, **remaining_overrides.pop(key)}
    client_config.update(remaining_overrides)


class _ResolvedClientConfig(dict):
    """Client config with request override provenance."""

    def __init__(self, config: dict[str, Any], overrides_applied: bool):
        super().__init__(config)
        self.overrides_applied = overrides_applied


def _resolve_client_config(
    client_config_overrides: Any = _CLIENT_CONFIG_OVERRIDES_UNSET,
) -> _ResolvedClientConfig:
    """Resolve a client config from environment settings and explicit overrides."""
    if client_config_overrides is _CLIENT_CONFIG_OVERRIDES_UNSET:
        overrides = _get_client_config_overrides()
    else:
        overrides = _snapshot_client_config_overrides(client_config_overrides)

    client_config = get_config().get_client_config()
    _apply_client_config_overrides(client_config, overrides)

    timeout_overridden = bool(overrides and "send_receive_timeout" in overrides)
    if "CLICKHOUSE_SEND_RECEIVE_TIMEOUT" not in os.environ and not timeout_overridden:
        query_timeout = get_mcp_config().query_timeout
        effective_timeout = client_config.get("send_receive_timeout", 300)
        if effective_timeout > query_timeout + 5:
            client_config["send_receive_timeout"] = query_timeout + 5

    return _ResolvedClientConfig(
        client_config,
        overrides_applied=bool(overrides),
    )


def _close_client(client) -> None:
    """Close a ClickHouse client without masking the caller's result."""
    try:
        client.close()
    except Exception:
        logger.debug("Failed to close ClickHouse client", exc_info=True)


def _retire_client_entry_locked(entry: _ClientCacheEntry):
    """Retire an entry and return its client when it can be closed now."""
    entry.retired = True
    if entry.active_users == 0 and not entry.closed:
        entry.closed = True
        return entry.client
    return None


def _release_client_entry(entry: _ClientCacheEntry) -> None:
    """Release a client lease and close a retired entry after its final user."""
    client_to_close = None
    with _client_cache_lock:
        if entry.active_users <= 0:
            raise RuntimeError("ClickHouse client cache entry released without a lease")
        entry.active_users -= 1
        if entry.retired and entry.active_users == 0 and not entry.closed:
            entry.closed = True
            client_to_close = entry.client
    if client_to_close is not None:
        _close_client(client_to_close)


def _evict_lru_entries_locked() -> List[Any]:
    """Retire least recently used entries until the cache is within its bound."""
    clients_to_close = []
    while len(_client_cache) > _CLIENT_CACHE_MAXSIZE:
        _, entry = _client_cache.popitem(last=False)
        client_to_close = _retire_client_entry_locked(entry)
        if client_to_close is not None:
            clients_to_close.append(client_to_close)
    return clients_to_close


def _evict_cached_client(config: dict, failed_client) -> bool:
    """Evict only the cached client instance that produced a connection error."""
    cache_key = _config_to_cache_key(config)
    if cache_key is None:
        return False
    client_to_close = None
    with _client_cache_lock:
        entry = _client_cache.get(cache_key)
        if entry is None or entry.client is not failed_client:
            return False
        _client_cache.pop(cache_key)
        client_to_close = _retire_client_entry_locked(entry)
    logger.info("Evicted stale cached ClickHouse client")
    if client_to_close is not None:
        _close_client(client_to_close)
    return True


def _return_client(client, config: dict):
    """Run base-client checks before returning a cached or new client."""
    overrides_applied = getattr(config, "overrides_applied", False)
    server_config = get_config()
    if (
        not overrides_applied
        and server_config.allow_write_access
        and not server_config.allow_drop
    ):
        _warn_if_overprivileged(client)
    return client


def _warn_for_native_protocol_port(config: dict) -> None:
    """Warn when the client is configured with a native protocol port."""
    port = config.get("port")
    if port in _NATIVE_PROTOCOL_PORTS:
        logger.warning(
            "CLICKHOUSE_PORT=%s is a native TCP protocol port (clickhouse-client). "
            "mcp-clickhouse uses the HTTP interface; prefer 8123 (HTTP) or 8443 (HTTPS).",
            port,
        )


def _prepare_client_entry(
    entry: _ClientCacheEntry, config: dict
) -> _ClientCacheEntry:
    """Run base-client checks while the caller holds a lease."""
    try:
        _return_client(entry.client, config)
    except Exception:
        _release_client_entry(entry)
        raise
    return entry


def _create_uncached_clickhouse_client(config: dict, *, cache_owned: bool):
    """Create and validate a ClickHouse client outside the cache lock."""
    config_fields = [
        f"secure={config['secure']}",
        f"verify={config['verify']}",
        f"connect_timeout={config['connect_timeout']}s",
        f"send_receive_timeout={config['send_receive_timeout']}s",
    ]
    if "server_host_name" in config:
        config_fields.append(f"server_host_name={config['server_host_name']}")
    logger.info(
        f"Creating ClickHouse client connection to {config['host']}:{config['port']} "
        f"as {config['username']} "
        f"({', '.join(config_fields)})"
    )

    try:
        connection_config = dict(config)
        if cache_owned:
            connection_config["autogenerate_session_id"] = False
        client = clickhouse_connect.get_client(**connection_config)
        version = client.server_version
        logger.info(f"Successfully connected to ClickHouse server version {version}")
        return client
    except Exception as e:
        message = _format_connection_failure(e, config)
        logger.error(message)
        raise


def _acquire_clickhouse_client(config: dict) -> _ClientCacheEntry:
    """Acquire a leased cached client, creating one when needed."""
    _warn_for_native_protocol_port(config)
    cache_key = _config_to_cache_key(config)
    if cache_key is None:
        client = _create_uncached_clickhouse_client(config, cache_owned=True)
        entry = _ClientCacheEntry(
            client=client,
            last_used=time.time(),
            active_users=1,
            retired=True,
        )
        return _prepare_client_entry(entry, config)

    candidate = None
    cached_entry = None
    with _client_cache_lock:
        entry = _client_cache.get(cache_key)
        if entry is not None and not entry.retired and not entry.closed:
            entry.active_users += 1
            if time.time() - entry.last_used > _CLIENT_IDLE_PING_THRESHOLD:
                candidate = entry
            else:
                entry.last_used = time.time()
                _client_cache.move_to_end(cache_key)
                cached_entry = entry
    if cached_entry is not None:
        logger.debug("Reusing cached client")
        return _prepare_client_entry(cached_entry, config)

    if candidate is not None:
        try:
            alive = candidate.client.ping()
        except Exception:
            alive = False

        replacement = None
        with _client_cache_lock:
            current = _client_cache.get(cache_key)
            if alive and current is candidate and not candidate.retired:
                candidate.last_used = time.time()
                _client_cache.move_to_end(cache_key)
            else:
                if current is candidate:
                    _client_cache.pop(cache_key)
                _retire_client_entry_locked(candidate)
                if current is not None and current is not candidate and not current.retired:
                    current.active_users += 1
                    current.last_used = time.time()
                    _client_cache.move_to_end(cache_key)
                    replacement = current

        if alive and replacement is None and not candidate.retired:
            logger.debug("Reusing cached client (ping OK after idle)")
            return _prepare_client_entry(candidate, config)

        _release_client_entry(candidate)
        if replacement is not None:
            logger.debug("Reusing cached client after concurrent replacement")
            return _prepare_client_entry(replacement, config)
        if not alive:
            logger.warning("Cached client failed ping, creating new client")

    client = _create_uncached_clickhouse_client(config, cache_owned=True)
    new_entry = _ClientCacheEntry(client=client, last_used=time.time(), active_users=1)
    winner = new_entry
    clients_to_close = []
    with _client_cache_lock:
        current = _client_cache.get(cache_key)
        if current is not None and not current.retired and not current.closed:
            current.active_users += 1
            current.last_used = time.time()
            _client_cache.move_to_end(cache_key)
            winner = current
            clients_to_close.append(client)
        else:
            if current is not None:
                _client_cache.pop(cache_key)
                client_to_close = _retire_client_entry_locked(current)
                if client_to_close is not None:
                    clients_to_close.append(client_to_close)
            _client_cache[cache_key] = new_entry
            clients_to_close.extend(_evict_lru_entries_locked())

    for client_to_close in clients_to_close:
        _close_client(client_to_close)

    return _prepare_client_entry(winner, config)


def create_clickhouse_client(
    client_config_overrides: Any = _CLIENT_CONFIG_OVERRIDES_UNSET,
    *,
    config: Optional[dict] = None,
):
    """Create an independently owned ClickHouse client for the given config."""
    if config is None:
        config = _resolve_client_config(client_config_overrides)
    elif client_config_overrides is not _CLIENT_CONFIG_OVERRIDES_UNSET:
        raise TypeError("Pass client_config_overrides or config, not both")

    _warn_for_native_protocol_port(config)
    client = _create_uncached_clickhouse_client(config, cache_owned=False)
    try:
        return _return_client(client, config)
    except Exception:
        _close_client(client)
        raise


def _clear_client_cache():
    """Retire all cached clients, closing those without active users."""
    clients_to_close = []
    with _client_cache_lock:
        for entry in _client_cache.values():
            client_to_close = _retire_client_entry_locked(entry)
            if client_to_close is not None:
                clients_to_close.append(client_to_close)
        _client_cache.clear()
    for client in clients_to_close:
        _close_client(client)


def _shutdown():
    # Drain every worker before closing the clients they may hold.
    QUERY_EXECUTOR.shutdown(wait=True)
    METADATA_EXECUTOR.shutdown(wait=True)
    CANCELLATION_EXECUTOR.shutdown(wait=True)
    HEALTH_EXECUTOR.shutdown(wait=True)
    _clear_client_cache()


atexit.register(_shutdown)


def build_query_settings(client) -> dict[str, str]:
    """Build query settings dict for ClickHouse queries.

    Always returns a dict (possibly empty) to ensure consistent behavior.
    """
    readonly_setting = get_readonly_setting(client)
    if readonly_setting is not None:
        return {"readonly": readonly_setting}
    return {}


def get_readonly_setting(client) -> Optional[str]:
    """Determine the readonly setting value for queries.

    This implements the following logic:
    1. If CLICKHOUSE_ALLOW_WRITE_ACCESS=true (writes enabled):
       - Allow writes if server permits (server readonly=None or "0")
       - Fall back to server's readonly setting if server enforces it
       - Log a warning when falling back

    2. If CLICKHOUSE_ALLOW_WRITE_ACCESS=false (default, read-only mode):
       - Enforce readonly=1 if server allows writes
       - Respect server's readonly setting if server enforces stricter mode

    Returns:
        "0" = writes allowed
        "1" = read-only mode (allows SET of non-privileged settings)
        "2" = strict read-only (server enforced; disallows SET)
        None = use server default (shouldn't happen in practice)
    """
    config = get_config()
    server_settings = getattr(client, "server_settings", {}) or {}
    server_readonly = _normalize_readonly_value(server_settings.get("readonly"))

    # Case 1: User wants write access (CLICKHOUSE_ALLOW_WRITE_ACCESS=true)
    if config.allow_write_access:
        if server_readonly in (None, "0"):
            logger.info("Write mode enabled (CLICKHOUSE_ALLOW_WRITE_ACCESS=true)")
            return "0"

        # If server forbids writes, respect server configuration
        logger.warning(
            "CLICKHOUSE_ALLOW_WRITE_ACCESS=true but server enforces readonly=%s; "
            "write operations will fail",
            server_readonly,
        )
        return server_readonly

    # Case 2: User wants read-only mode (CLICKHOUSE_ALLOW_WRITE_ACCESS=false, default)
    if server_readonly in (None, "0"):
        return "1"  # Enforce read-only since server allows writes

    return server_readonly  # Server already enforces readonly, respect it


def _normalize_readonly_value(value: Any) -> Optional[str]:
    """Normalize ClickHouse readonly setting to a simple string.

    The clickhouse_connect library represents settings as objects with a .value attribute.
    This function extracts the actual value for our logic.

    Args:
        value: The readonly setting value from ClickHouse server. Can be:
            - None (server has no readonly restriction)
            - A clickhouse_connect setting object with a .value attribute
            - An int (0, 1, 2)
            - A str ("0", "1", "2")

    Returns:
        Optional[str]: Normalized readonly value as string ("0", "1", "2") or None
    """
    if value is None:
        return None

    # Extract value from clickhouse_connect setting object
    if hasattr(value, "value"):
        value = value.value

    return str(value)


def create_chdb_client():
    """Create a chDB client connection."""
    if not get_chdb_config().enabled:
        raise ValueError("chDB is not enabled. Set CHDB_ENABLED=true to enable it.")
    if _chdb_client is None:
        raise RuntimeError(_chdb_error_message or "chDB client is not available.")
    return _chdb_client


def execute_chdb_query(query: str):
    """Execute a query using chDB client."""
    client = create_chdb_client()
    try:
        res = client.query(query, "JSON")
        if res.has_error():
            error_msg = res.error_message()
            logger.error(f"Error executing chDB query: {error_msg}")
            return {"error": error_msg}

        result_data = res.data()
        if not result_data:
            return []

        result_json = json.loads(result_data)

        return result_json.get("data", [])

    except Exception as err:
        logger.error(f"Error executing chDB query: {err}")
        return {"error": str(err)}


def _process_chdb_result(result) -> str:
    if isinstance(result, dict) and "error" in result:
        logger.warning(f"chDB query failed: {result['error']}")
        return _serialize_tool_result({
            "status": "error",
            "message": f"chDB query failed: {result['error']}",
        })
    return _serialize_tool_result(result)


def run_chdb_select_query(query: str) -> str:
    """Run SQL in chDB, an in-process ClickHouse engine"""
    logger.info(f"Executing chDB SELECT query: {query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_chdb_query, query)
        timeout_secs = get_mcp_config().query_timeout
        try:
            result = future.result(timeout=timeout_secs)
            return _process_chdb_result(result)
        except concurrent.futures.TimeoutError:
            logger.warning(f"chDB query timed out after {timeout_secs} seconds: {query}")
            future.cancel()
            return _serialize_tool_result({
                "status": "error",
                "message": f"chDB query timed out after {timeout_secs} seconds",
            })
    except Exception as e:
        logger.error(f"Unexpected error in run_chdb_select_query: {e}")
        return _serialize_tool_result({"status": "error", "message": f"Unexpected error: {e}"})


async def run_chdb_select_query_async(query: str) -> str:
    """Async MCP-facing wrapper for chDB queries."""
    logger.info(f"Executing chDB SELECT query: {query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_chdb_query, query)
        timeout_secs = get_mcp_config().query_timeout
        try:
            result = await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=timeout_secs
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"chDB query timed out after {timeout_secs} seconds: {query}"
            )
            future.cancel()
            return _serialize_tool_result({
                "status": "error",
                "message": f"chDB query timed out after {timeout_secs} seconds",
            })

        return await asyncio.to_thread(_process_chdb_result, result)
    except Exception as e:
        logger.error(f"Unexpected error in run_chdb_select_query_async: {e}")
        return _serialize_tool_result({"status": "error", "message": f"Unexpected error: {e}"})


def chdb_initial_prompt() -> str:
    """This prompt helps users understand how to interact and perform common operations in chDB"""
    return CHDB_PROMPT


def _init_chdb_client():
    """Initialize the global chDB client instance."""
    global _chdb_error_message
    try:
        if not get_chdb_config().enabled:
            logger.info("chDB is disabled, skipping client initialization")
            _chdb_error_message = None
            return None

        client_config = get_chdb_config().get_client_config()
        data_path = client_config["data_path"]
        logger.info(f"Creating chDB client with data_path={data_path}")
        import chdb.session as chs

        client = chs.Session(path=data_path)
        _chdb_error_message = None
        logger.info(f"Successfully connected to chDB with data_path={data_path}")
        return client
    except ModuleNotFoundError as e:
        if e.name in {"chdb", "chdb.session"}:
            _chdb_error_message = (
                "chDB support requires the optional dependency. "
                "Install mcp-clickhouse[chdb] to enable chDB features."
            )
            logger.warning(_chdb_error_message)
            return None
        _chdb_error_message = f"Failed to initialize chDB client: {e}"
        logger.error(_chdb_error_message)
        return None
    except ImportError as e:
        _chdb_error_message = f"Failed to initialize chDB client: {e}"
        logger.error(_chdb_error_message)
        return None
    except Exception as e:
        _chdb_error_message = f"Failed to initialize chDB client: {e}"
        logger.error(_chdb_error_message)
        return None


def _register_chdb_tools():
    """Register chDB tools when the feature is enabled and available.

    Note: This function is not idempotent. Calling it multiple times will
    register duplicate tools. It is intended to be called once at module load.
    """
    global _chdb_client
    if not get_chdb_config().enabled:
        return

    _chdb_client = _init_chdb_client()
    if _chdb_client is None:
        logger.warning("chDB is enabled but unavailable; skipping chDB tool registration")
        return

    atexit.register(_chdb_client.close)
    mcp.add_tool(
        Tool.from_function(
            run_chdb_select_query_async,
            name="run_chdb_select_query",
            description=(
                "Run SQL in chDB, an in-process ClickHouse engine. Integers outside "
                "[-9007199254740991, 9007199254740991] are returned as decimal strings."
            ),
        )
    )
    chdb_prompt = Prompt.from_function(
        chdb_initial_prompt,
        name="chdb_initial_prompt",
        description="This prompt helps users understand how to interact and perform common operations in chDB",
    )
    mcp.add_prompt(chdb_prompt)
    logger.info("chDB tools and prompts registered")


if os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true":
    mcp.add_tool(Tool.from_function(list_databases_async, name="list_databases"))
    mcp.add_tool(Tool.from_function(list_tables_async, name="list_tables"))
    mcp.add_tool(
        Tool.from_function(
            run_query_async,
            name="run_query",
            description=(
                "Execute SQL queries in ClickHouse. Queries run in read-only mode by default. "
                "Set CLICKHOUSE_ALLOW_WRITE_ACCESS=true to allow DDL and DML operations. "
                "Set CLICKHOUSE_ALLOW_DROP=true to additionally allow destructive operations "
                "(DROP, TRUNCATE, DELETE, UPDATE, REPLACE TABLE/PARTITION, CREATE OR REPLACE, "
                "CLEAR COLUMN/INDEX/PROJECTION, DETACH PERMANENTLY). That gate is a best-effort "
                "accident guard, not a security boundary. Integers outside "
                "[-9007199254740991, 9007199254740991] are returned as decimal strings."
            ),
        )
    )
    logger.info("ClickHouse tools registered")


_register_chdb_tools()
