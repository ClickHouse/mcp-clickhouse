import importlib
import inspect
import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Dict, Optional

from dotenv import dotenv_values, load_dotenv
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.utilities.auth import parse_scopes
from pydantic import TypeAdapter

from mcp_clickhouse.mcp_env import TransportType

logger = logging.getLogger("mcp-clickhouse")

_HTTP_TRANSPORTS = (TransportType.HTTP.value, "streamable-http", TransportType.SSE.value)

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
_AUTH_INT_ADAPTER: TypeAdapter[int]
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


def _initialize_auth_parser() -> None:
    """Preserve dotenv-dependent Pydantic initialization timing."""
    global _AUTH_INT_ADAPTER
    _AUTH_INT_ADAPTER = TypeAdapter(int)


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
