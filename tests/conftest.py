"""Shared fixtures for the mcp-clickhouse test suite.

Module-level helpers (fake clients, JSON-RPC request bodies, the recording ASGI
app) live in tests/helpers.py so test modules can import them by name; this file
holds only fixtures and the import-time environment default below.
"""

import os

# fastmcp.settings is built when fastmcp is first imported, so the camelCase
# compatibility bridge must be switched off before any test module imports it.
# CI sets the same variable explicitly; this makes local runs match without it.
# See MIGRATION_DECISIONS.md D6.
os.environ.setdefault("FASTMCP_MCP_CAMELCASE_COMPAT", "false")

import pytest  # noqa: E402

from mcp_clickhouse import mcp_server as mcp_server_module  # noqa: E402
from tests.helpers import HTTP_ENV_VARS  # noqa: E402


@pytest.fixture
def mcp_server():
    """The module singleton FastMCP server."""
    return mcp_server_module.mcp


@pytest.fixture(autouse=True)
def reset_server_state():
    """Clear module-level server state before and after every test.

    The client cache is retired through _clear_client_cache so cached clients are
    closed rather than leaked. Tests that assert on cache contents do so within a
    single test, so clearing at the boundaries does not affect them.
    """

    def reset():
        mcp_server_module._clear_client_cache()
        with mcp_server_module._active_queries_lock:
            mcp_server_module._active_queries.clear()
        mcp_server_module.table_pagination_cache.clear()
        mcp_server_module._grants_advisory_done = False

    reset()
    yield
    reset()


@pytest.fixture
def clean_http_env(monkeypatch: pytest.MonkeyPatch):
    """Unset every MCP transport, auth, and Host/Origin variable."""
    for name in HTTP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def authenticated_app_env(clean_http_env, monkeypatch: pytest.MonkeyPatch):
    """A static-token HTTP app configuration for Starlette TestClient requests."""
    monkeypatch.setenv("CLICKHOUSE_ENABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_ORIGINS", "http://client.example")
