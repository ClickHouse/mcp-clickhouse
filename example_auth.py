"""Example authentication module for mcp-clickhouse.

FastMCP 3 and later no longer build auth providers from FASTMCP_SERVER_AUTH_*
environment variables. Instead, point CLICKHOUSE_MCP_AUTH_MODULE at an
importable module like this one that defines create_auth_provider() and returns
any fastmcp.server.auth.AuthProvider instance.

Usage:
    export CLICKHOUSE_MCP_SERVER_TRANSPORT=http
    export CLICKHOUSE_MCP_AUTH_MODULE=example_auth
    export MCP_AUTH_JWKS_URI="https://login.example.com/.well-known/jwks.json"
    export MCP_AUTH_ISSUER="https://login.example.com/"
    export MCP_AUTH_AUDIENCE="mcp-clickhouse"
    mcp-clickhouse

The module must be importable: put it on PYTHONPATH or install it as a package.
The variable names below (MCP_AUTH_*) are only a convention for this example;
read whatever configuration your deployment provides.

OAuth providers work the same way with explicit keyword arguments, for example:

    from fastmcp.server.auth.providers.azure import AzureProvider

    def create_auth_provider():
        return AzureProvider(
            client_id=os.environ["AZURE_CLIENT_ID"],
            client_secret=os.environ["AZURE_CLIENT_SECRET"],
            tenant_id=os.environ["AZURE_TENANT_ID"],
            base_url=os.environ["MCP_PUBLIC_URL"],
            required_scopes=["User.Read"],
        )

See https://gofastmcp.com/servers/auth for the available providers and their
constructor arguments.
"""

import os

from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} must be set when using example_auth as CLICKHOUSE_MCP_AUTH_MODULE")
    return value


def create_auth_provider() -> AuthProvider:
    """Validate bearer JWTs issued by an identity provider via its JWKS endpoint."""
    return JWTVerifier(
        jwks_uri=_require_env("MCP_AUTH_JWKS_URI"),
        issuer=_require_env("MCP_AUTH_ISSUER"),
        audience=_require_env("MCP_AUTH_AUDIENCE"),
        required_scopes=[],
    )
