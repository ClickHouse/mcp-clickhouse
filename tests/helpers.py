"""Helpers shared across test modules.

Import what you need by name, for example ``from tests.helpers import
fake_clickhouse_client``. Fixtures live in tests/conftest.py.
"""

import asyncio
import json
import re
import sys
import types
from typing import Any, Dict, Optional, Tuple
from unittest.mock import MagicMock

import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

# Every variable that selects or secures the HTTP transport. Scrubbing all of them
# gives each HTTP test a known starting point; tests set what they need explicitly.
HTTP_ENV_VARS = (
    "CLICKHOUSE_MCP_ALLOWED_HOSTS",
    "CLICKHOUSE_MCP_ALLOWED_ORIGINS",
    "CLICKHOUSE_MCP_TRUSTED_PROXIES",
    "CLICKHOUSE_MCP_AUTH_DISABLED",
    "CLICKHOUSE_MCP_AUTH_MODULE",
    "CLICKHOUSE_MCP_AUTH_TOKEN",
    "CLICKHOUSE_MCP_BIND_HOST",
    "CLICKHOUSE_MCP_BIND_PORT",
    "FASTMCP_SERVER_AUTH",
)

MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


def clear_http_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every variable in HTTP_ENV_VARS (see the clean_http_env fixture)."""
    for name in HTTP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def initialize_request(protocol_version: str = MCP_PROTOCOL_VERSION) -> dict:
    """A JSON-RPC initialize request body for the streamable HTTP endpoint."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1"},
        },
    }


INITIALIZE_REQUEST = initialize_request()


def jsonrpc_body(response) -> dict:
    """Decode a JSON or single-event event-stream response body from the MCP endpoint."""
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    match = re.search(r"^data: (.*)$", response.text, re.M)
    assert match is not None, response.text
    return json.loads(match.group(1))


def fake_clickhouse_client(server_version: str = "24.1", **attrs: Any) -> MagicMock:
    """A MagicMock standing in for a clickhouse_connect client.

    ``server_version`` is read by the client cache on creation; extra keyword
    arguments are set as attributes (``server_settings={}``, ``name="stale"``).
    """
    client = MagicMock(server_version=server_version, **attrs)
    return client


def static_token_provider(token: str = "module-token") -> StaticTokenVerifier:
    """A StaticTokenVerifier accepting one bearer token, as an auth module would build."""
    return StaticTokenVerifier(
        tokens={token: {"client_id": "module-client", "scopes": []}},
        required_scopes=[],
    )


def install_auth_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> None:
    """Register an in-memory module so CLICKHOUSE_MCP_AUTH_MODULE can import it.

    Sets the given attributes on the module (normally ``create_auth_provider``).
    The caller sets CLICKHOUSE_MCP_AUTH_MODULE when the test needs it.
    """
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


class RecordingApp:
    """Inner ASGI app that records the scopes it receives and answers 200."""

    def __init__(self):
        self.scopes = []

    @property
    def called(self) -> bool:
        return bool(self.scopes)

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"reached"})


async def send_asgi_request_async(
    middleware,
    *,
    path: str = "/mcp",
    headers=None,
    scope_type: str = "http",
    method: str = "POST",
    client: Optional[Tuple[str, int]] = None,
    include_client: bool = False,
) -> Tuple[int, bytes]:
    """Drive one request through an ASGI middleware, returning (status, body).

    ``headers`` may be a mapping or a list of pairs (to send duplicate fields).
    The ``client`` key is added to the scope only when ``include_client`` is set
    or a client is given, so a request without a peer address can be modelled.
    """
    if isinstance(headers, dict):
        headers = list(headers.items())
    raw_headers = [(name.lower().encode(), value.encode()) for name, value in (headers or [])]
    scope: Dict[str, Any] = {
        "type": scope_type,
        "method": method,
        "path": path,
        "headers": raw_headers,
    }
    if include_client or client is not None:
        scope["client"] = client
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await middleware(scope, receive, send)

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


def send_asgi_request(middleware, **kwargs) -> Tuple[int, bytes]:
    """Synchronous wrapper around send_asgi_request_async."""
    return asyncio.run(send_asgi_request_async(middleware, **kwargs))
