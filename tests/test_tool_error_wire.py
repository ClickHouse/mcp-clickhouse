"""Wire-level tests pinning FastMCP 4's contract for tool-level errors.

FastMCP 4 masks exceptions raised out of a running tool (fastmcp.exceptions.ToolError,
and pydantic ValidationError from bad arguments) into a JSON-RPC *result* object
carrying isError=true, not a JSON-RPC error object. This was verified empirically
against a real streamable HTTP round trip (see the class below) and against
fastmcp/server/server.py's `_mcp_call_tool`: exceptions raised out of `tool._run(...)`
propagate up as `ToolError` and are turned into an `isError` CallToolResult by the
handler adapter around it. Nothing in that path calls
`fastmcp.exceptions.to_mcp_error`, which maps to JSON-RPC error codes such as
INTERNAL_ERROR (-32603) or INVALID_PARAMS (-32602); that mapping is reserved for
requests that never reach a tool call at all (unknown method, a request that fails
before the interior dispatch runs, or a missing-client-capability error under
SEP-2575, which is deliberately not masked). A timeout or validation failure inside
`tools/call` is always a `result.isError` on the wire, never a top-level `error` key.

Existing tests (tests/test_query_cancellation.py, tests/test_pagination_validation.py)
already pin the ToolError message text and the in-memory Client.call_tool raise
behavior. These tests add the wire-shape assertions those do not cover.
"""

import concurrent.futures
import warnings
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastmcp import Client
from starlette.exceptions import StarletteDeprecationWarning

with warnings.catch_warnings():
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient

import mcp_clickhouse.mcp_server as mcp_server
from tests.helpers import INITIALIZE_REQUEST, MCP_HEADERS, clear_http_env, jsonrpc_body


class TestRunQueryTimeoutInMemory:
    """The registered run_query tool, called through fastmcp.Client in-memory."""

    @pytest.mark.asyncio
    async def test_run_query_timeout_is_error_with_message(self):
        """A timed-out run_query call surfaces as is_error with the timeout text.

        The future submitted to QUERY_EXECUTOR is left pending (never resolved),
        so asyncio.wait_for's tiny patched timeout is what ends the call; no
        wall-clock sleep is used beyond that timeout itself.
        """
        pending_query = concurrent.futures.Future()

        with (
            patch("mcp_clickhouse.mcp_server._resolve_client_config", return_value={}),
            patch(
                "mcp_clickhouse.mcp_server.get_mcp_config",
                return_value=SimpleNamespace(query_timeout=0.02),
            ),
            patch("mcp_clickhouse.mcp_server.QUERY_EXECUTOR") as query_executor,
        ):
            query_executor.submit.return_value = pending_query
            async with Client(mcp_server.mcp) as client:
                result = await client.call_tool(
                    "run_query",
                    {"query": "SELECT sleep(999)"},
                    raise_on_error=False,
                )

        assert result.is_error is True
        assert "timed out" in result.content[0].text


class TestToolErrorHttpWire:
    """Drive the real streamable HTTP app to observe the raw JSON-RPC shape."""

    def _init_session(self, client):
        init = client.post("/mcp", json=INITIALIZE_REQUEST, headers=self._headers())
        assert init.status_code == 200
        session_headers = {
            **self._headers(),
            "mcp-session-id": init.headers["mcp-session-id"],
        }
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
        )
        return session_headers

    @staticmethod
    def _headers():
        return {**MCP_HEADERS, "authorization": "Bearer wire-test-token"}

    def test_run_query_timeout_is_isError_result_not_json_rpc_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A timeout ToolError arrives as a JSON-RPC result with isError, not an error."""
        clear_http_env(monkeypatch)
        monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "wire-test-token")
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")

        app = mcp_server.mcp.http_app(transport="http")
        with TestClient(app) as client:
            session_headers = self._init_session(client)

            pending_query = concurrent.futures.Future()
            with (
                patch(
                    "mcp_clickhouse.mcp_server.get_mcp_config",
                    return_value=SimpleNamespace(query_timeout=0.02, trusted_proxies=None),
                ),
                patch("mcp_clickhouse.mcp_server.QUERY_EXECUTOR") as query_executor,
            ):
                query_executor.submit.return_value = pending_query
                response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "run_query",
                            "arguments": {"query": "SELECT sleep(999)"},
                        },
                    },
                    headers=session_headers,
                )

        assert response.status_code == 200
        body = jsonrpc_body(response)
        assert "error" not in body, body
        assert body["result"]["isError"] is True
        text = body["result"]["content"][0]["text"]
        assert "timed out" in text

    def test_list_tables_validation_error_is_isError_result_names_the_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A bad page_size surfaces as isError over the wire and names list_tables.

        tests/test_pagination_validation.py already pins this through the in-memory
        Client (raise_on_error=True raises ToolError naming list_tables). This adds
        the missing wire-level HTTP variant: a raw JSON-RPC result, not an error.
        """
        clear_http_env(monkeypatch)
        monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "wire-test-token")
        monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")

        app = mcp_server.mcp.http_app(transport="http")
        with patch("mcp_clickhouse.mcp_server._acquire_clickhouse_client") as acquire_client:
            with TestClient(app) as client:
                session_headers = self._init_session(client)
                response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "list_tables",
                            "arguments": {"database": "database", "page_size": 0},
                        },
                    },
                    headers=session_headers,
                )

        assert response.status_code == 200
        body = jsonrpc_body(response)
        assert "error" not in body, body
        assert body["result"]["isError"] is True
        text = body["result"]["content"][0]["text"]
        assert "list_tables" in text
        assert "list_tables_async" not in text
        acquire_client.assert_not_called()
