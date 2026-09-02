"""MCP 2026-07-28 and legacy protocol compatibility tests."""

import asyncio
import json
import socket
from importlib.metadata import PackageNotFoundError, version as package_version
from unittest.mock import patch

import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import SSETransport
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from mcp_clickhouse.mcp_server import (
    _active_queries,
    _active_queries_lock,
    _get_mcp_server_version,
    _remove_active_query,
    mcp,
)

_MODERN_VERSION = "2026-07-28"
_LEGACY_VERSION = "2025-11-25"
_CONTENT_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}
_CLICKHOUSE_TOOL_NAMES = ["list_databases", "list_tables", "run_query"]


def _assert_registered_tool_names(tool_names: list[str]) -> None:
    expected = list(_CLICKHOUSE_TOOL_NAMES)
    if "run_chdb_select_query" in tool_names:
        expected.append("run_chdb_select_query")
    assert tool_names == expected


def _modern_request(method: str, *, params: dict | None = None, request_id: int = 1) -> dict:
    request_params = dict(params or {})
    request_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": _MODERN_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": request_params,
    }


def _modern_headers(method: str, *, name: str | None = None) -> dict[str, str]:
    headers = {
        **_CONTENT_HEADERS,
        "MCP-Protocol-Version": _MODERN_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def _sse_data(response) -> dict:
    data_line = next(line for line in response.text.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


def _completed_query(_query, query_id, _config):
    with _active_queries_lock:
        state = _active_queries[query_id]
    _remove_active_query(query_id, state)
    return '{"columns":["value"],"rows":[[1]]}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_version"),
    [("auto", _MODERN_VERSION), ("legacy", _LEGACY_VERSION)],
)
async def test_in_memory_client_supports_modern_and_legacy_eras(mode, expected_version):
    async with Client(mcp, mode=mode) as client:
        assert client.session.protocol_version == expected_version
        tools = await client.list_tools()
        with pytest.raises(ToolError, match="page_size"):
            await client.call_tool(
                "list_tables",
                {"database": "system", "page_size": 0},
            )

    _assert_registered_tool_names([tool.name for tool in tools])


def test_modern_http_discover_list_and_tool_error_without_initialize(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    app = mcp.http_app(transport="http")

    with TestClient(app) as client:
        discover = client.post(
            "/mcp",
            headers=_modern_headers("server/discover"),
            json=_modern_request("server/discover"),
        )
        tools_list = client.post(
            "/mcp",
            headers=_modern_headers("tools/list"),
            json=_modern_request("tools/list", request_id=2),
        )
        tool_error = client.post(
            "/mcp",
            headers=_modern_headers("tools/call", name="list_tables"),
            json=_modern_request(
                "tools/call",
                params={
                    "name": "list_tables",
                    "arguments": {"database": "system", "page_size": 0},
                },
                request_id=3,
            ),
        )
        with patch(
            "mcp_clickhouse.mcp_server.execute_query",
            side_effect=_completed_query,
        ):
            tool_success = client.post(
                "/mcp",
                headers=_modern_headers("tools/call", name="run_query"),
                json=_modern_request(
                    "tools/call",
                    params={"name": "run_query", "arguments": {"query": "SELECT 1"}},
                    request_id=4,
                ),
            )

    assert discover.status_code == 200
    discover_result = discover.json()["result"]
    assert _MODERN_VERSION in discover_result["supportedVersions"]
    assert discover_result["resultType"] == "complete"
    assert isinstance(discover_result["ttlMs"], int)
    assert discover_result["ttlMs"] >= 0
    assert discover_result["cacheScope"] in {"public", "private"}
    assert discover_result["capabilities"]["tools"] == {"listChanged": False}
    assert discover_result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == (
        "mcp-clickhouse"
    )
    assert discover_result["_meta"]["io.modelcontextprotocol/serverInfo"][
        "version"
    ] == package_version("mcp-clickhouse")
    assert "https://github.com/ClickHouse/agent-skills" in discover_result["instructions"]
    assert "mcp-session-id" not in discover.headers

    assert tools_list.status_code == 200
    tools_result = tools_list.json()["result"]
    _assert_registered_tool_names([tool["name"] for tool in tools_result["tools"]])
    assert tools_result["resultType"] == "complete"
    assert isinstance(tools_result["ttlMs"], int)
    assert tools_result["ttlMs"] >= 0
    assert tools_result["cacheScope"] in {"public", "private"}
    assert "mcp-session-id" not in tools_list.headers

    assert tool_error.status_code == 200
    tool_result = tool_error.json()["result"]
    assert tool_result["isError"] is True
    assert tool_result["resultType"] == "complete"
    assert "page_size" in tool_result["content"][0]["text"]
    assert "mcp-session-id" not in tool_error.headers

    assert tool_success.status_code == 200
    success_result = tool_success.json()["result"]
    assert success_result["isError"] is False
    assert success_result["resultType"] == "complete"
    assert json.loads(success_result["content"][0]["text"])["rows"] == [[1]]
    assert "mcp-session-id" not in tool_success.headers


def test_legacy_http_initialize_and_list_tools(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    app = mcp.http_app(transport="http")
    initialize_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _LEGACY_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "raw-test-client", "version": "1"},
        },
    }

    with TestClient(app) as client:
        initialize = client.post(
            "/mcp",
            headers=_CONTENT_HEADERS,
            json=initialize_request,
        )
        session_id = initialize.headers["mcp-session-id"]
        session_headers = {
            **_CONTENT_HEADERS,
            "MCP-Protocol-Version": _LEGACY_VERSION,
            "Mcp-Session-Id": session_id,
        }
        initialized = client.post(
            "/mcp",
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        tools_list = client.post(
            "/mcp",
            headers=session_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

    assert initialize.status_code == 200
    initialize_result = _sse_data(initialize)["result"]
    assert initialize_result["protocolVersion"] == _LEGACY_VERSION
    assert initialize_result["serverInfo"]["name"] == "mcp-clickhouse"
    assert initialize_result["serverInfo"]["version"] == package_version("mcp-clickhouse")
    assert initialized.status_code == 202
    assert tools_list.status_code == 200
    _assert_registered_tool_names(
        [tool["name"] for tool in _sse_data(tools_list)["result"]["tools"]]
    )


@pytest.mark.asyncio
async def test_legacy_sse_client_initialize_and_list_tools(
    monkeypatch: pytest.MonkeyPatch,
):
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen()
    port = server_socket.getsockname()[1]
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", f"127.0.0.1:{port}")
    app = mcp.sse_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            lifespan="on",
            proxy_headers=False,
            ws="none",
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[server_socket]))

    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started

        transport = SSETransport(
            f"http://127.0.0.1:{port}/sse",
            headers={"Authorization": "Bearer test-token"},
        )
        async with Client(transport, mode="legacy", timeout=5) as client:
            assert client.session.protocol_version == _LEGACY_VERSION
            tools = await client.list_tools()

        _assert_registered_tool_names([tool.name for tool in tools])
    finally:
        server.should_exit = True
        await server_task
        server_socket.close()


@pytest.mark.parametrize(
    ("case", "headers", "request_body", "expected_status", "expected_code"),
    [
        (
            "missing method header",
            {**_CONTENT_HEADERS, "MCP-Protocol-Version": _MODERN_VERSION},
            _modern_request("tools/list"),
            400,
            -32020,
        ),
        (
            "version mismatch",
            _modern_headers("tools/list"),
            {
                **_modern_request("tools/list"),
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": _LEGACY_VERSION,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
            400,
            -32020,
        ),
        (
            "unsupported version",
            {
                **_CONTENT_HEADERS,
                "MCP-Protocol-Version": "2099-01-01",
                "Mcp-Method": "tools/list",
            },
            {
                **_modern_request("tools/list"),
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2099-01-01",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
            400,
            -32022,
        ),
        (
            "missing tool name header",
            _modern_headers("tools/call"),
            _modern_request(
                "tools/call",
                params={"name": "list_tables", "arguments": {"database": "system"}},
            ),
            400,
            -32020,
        ),
        (
            "unknown method",
            _modern_headers("unknown/method"),
            _modern_request("unknown/method"),
            404,
            -32601,
        ),
        (
            "removed initialize method",
            _modern_headers("initialize"),
            _modern_request("initialize"),
            404,
            -32601,
        ),
    ],
)
def test_modern_http_header_and_method_errors(
    monkeypatch: pytest.MonkeyPatch,
    case,
    headers,
    request_body,
    expected_status,
    expected_code,
):
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    app = mcp.http_app(transport="http")

    with TestClient(app) as client:
        response = client.post("/mcp", headers=headers, json=request_body)

    assert response.status_code == expected_status, case
    response_body = response.json()
    assert response_body["id"] == request_body["id"], case
    assert response_body["error"]["code"] == expected_code, case
    if case == "unsupported version":
        assert response_body["error"]["data"]["supported"] == [_MODERN_VERSION]
        assert response_body["error"]["data"]["requested"] == "2099-01-01"


def test_modern_http_requires_request_metadata(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    app = mcp.http_app(transport="http")
    request = _modern_request("tools/list")
    request["params"]["_meta"] = {}

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=_modern_headers("tools/list"),
            json=request,
        )

    assert response.status_code == 400
    response_body = response.json()
    assert response_body["id"] == request["id"]
    assert response_body["error"]["code"] == -32602


def test_modern_http_decodes_mcp_name_base64_sentinel(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    app = mcp.http_app(transport="http")
    request = _modern_request(
        "tools/call",
        params={"name": "run_query", "arguments": {"query": "SELECT 1"}},
        request_id=91,
    )
    headers = _modern_headers("tools/call", name="=?base64?cnVuX3F1ZXJ5?=")

    with (
        patch(
            "mcp_clickhouse.mcp_server.execute_query",
            side_effect=_completed_query,
        ),
        TestClient(app) as client,
    ):
        response = client.post("/mcp", headers=headers, json=request)

    assert response.status_code == 200
    assert response.json()["id"] == 91
    assert response.json()["result"]["isError"] is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "MCP Python SDK 2.1.1 uses header-first dual-era dispatch; body-first detection "
        "would preserve the modern request and return HeaderMismatch"
    ),
)
def test_modern_http_missing_protocol_header_is_header_mismatch(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_MCP_ALLOWED_HOSTS", "testserver")
    app = mcp.http_app(transport="http")
    request = _modern_request("tools/list", request_id=92)

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={**_CONTENT_HEADERS, "Mcp-Method": "tools/list"},
            json=request,
        )

    assert response.status_code == 400
    response_body = response.json()
    assert response_body["id"] == request["id"]
    assert response_body["error"]["code"] == -32020


def test_server_version_falls_back_when_distribution_metadata_is_missing():
    with patch(
        "mcp_clickhouse.mcp_server.package_version",
        side_effect=PackageNotFoundError,
    ):
        fallback_version = _get_mcp_server_version()

    assert fallback_version == "unknown"
    assert FastMCP("test-server", version=fallback_version).version == "unknown"
