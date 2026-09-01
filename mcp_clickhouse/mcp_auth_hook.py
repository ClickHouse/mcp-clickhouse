"""Optional user-provided authentication provider loading."""

import importlib
import logging

from fastmcp.server.auth import AuthProvider

logger = logging.getLogger("mcp-clickhouse")

AUTH_PROVIDER_FACTORY = "create_auth_provider"


def load_auth_provider(module_path: str) -> AuthProvider:
    """Import `module_path` and build its FastMCP AuthProvider.

    The module must define a callable `create_auth_provider()` that returns an
    instance of `fastmcp.server.auth.AuthProvider`. The module is trusted code
    supplied by the operator; this function validates the shape of what it
    returns and fails clearly, without echoing provider configuration.
    """
    logger.info("Loading authentication module: %s", module_path)
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ImportError(
            f"Failed to import CLICKHOUSE_MCP_AUTH_MODULE '{module_path}': {e}"
        ) from e

    factory = getattr(module, AUTH_PROVIDER_FACTORY, None)
    if not callable(factory):
        raise ValueError(
            f"CLICKHOUSE_MCP_AUTH_MODULE '{module_path}' must define a callable "
            f"{AUTH_PROVIDER_FACTORY}() that returns a fastmcp.server.auth.AuthProvider"
        )

    provider = factory()
    if not isinstance(provider, AuthProvider):
        raise ValueError(
            f"{module_path}.{AUTH_PROVIDER_FACTORY}() returned "
            f"{type(provider).__name__}, expected a fastmcp.server.auth.AuthProvider instance"
        )
    return provider
