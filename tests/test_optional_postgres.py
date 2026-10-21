"""Tests for optional Postgres driver loading, tool registration, and health reporting."""

import builtins
import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from starlette.requests import Request

from mcp_clickhouse import mcp_server

_POSTGRES_ENV = {"POSTGRES_ENABLED": "true", "POSTGRES_HOST": "localhost", "POSTGRES_USER": "u"}


@pytest.fixture(autouse=True)
def restore_postgres_state(monkeypatch):
    backend = mcp_server._postgres_backend
    monkeypatch.setattr(backend, "psycopg", backend.psycopg)
    monkeypatch.setattr(backend, "error_message", backend.error_message)


def _raising_import(error: ImportError):
    real_import = builtins.__import__

    def raising_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "psycopg":
            raise error
        return real_import(name, globals, locals, fromlist, level)

    return raising_import


def test_init_driver_surfaces_optional_dependency_message():
    error = ModuleNotFoundError("No module named 'psycopg'")
    error.name = "psycopg"
    with patch("builtins.__import__", side_effect=_raising_import(error)):
        assert mcp_server._postgres_backend._init_driver() is False

    assert mcp_server._postgres_backend.psycopg is None
    assert "mcp-clickhouse[postgres]" in mcp_server._postgres_backend.error_message


def test_init_driver_treats_other_import_errors_as_init_failures():
    error = ImportError("no pq wrapper available")
    with patch("builtins.__import__", side_effect=_raising_import(error)):
        assert mcp_server._postgres_backend._init_driver() is False

    message = mcp_server._postgres_backend.error_message
    assert "Failed to initialize the Postgres driver" in message
    assert "mcp-clickhouse[postgres]" not in message


def test_init_driver_requires_pipeline_support():
    pytest.importorskip("psycopg")
    with patch("psycopg.Pipeline.is_supported", return_value=False):
        assert mcp_server._postgres_backend._init_driver() is False

    assert "pipeline mode" in mcp_server._postgres_backend.error_message


def test_register_postgres_tools_skips_when_disabled():
    with (
        patch.dict("os.environ", {"POSTGRES_ENABLED": "false"}, clear=False),
        patch.object(mcp_server._postgres_backend, "_init_driver") as init,
        patch.object(mcp_server.mcp, "add_tool") as add_tool,
    ):
        mcp_server._register_postgres_tools()

    init.assert_not_called()
    add_tool.assert_not_called()


def test_register_postgres_tools_skips_when_driver_is_unavailable():
    with (
        patch.dict("os.environ", _POSTGRES_ENV, clear=False),
        patch.object(mcp_server._postgres_backend, "_init_driver", return_value=False),
        patch.object(mcp_server.mcp, "add_tool") as add_tool,
    ):
        mcp_server._register_postgres_tools()

    add_tool.assert_not_called()


def test_register_postgres_tools_registers_three_tools():
    with (
        patch.dict("os.environ", _POSTGRES_ENV, clear=False),
        patch.object(mcp_server._postgres_backend, "_init_driver", return_value=True),
        patch.object(mcp_server.mcp, "add_tool") as add_tool,
    ):
        mcp_server._register_postgres_tools()

    names = [call.args[0].name for call in add_tool.call_args_list]
    assert names == ["list_postgres_schemas", "list_postgres_tables", "run_postgres_query"]


@pytest.mark.asyncio
async def test_health_check_reports_postgres_only_server_as_ok():
    request = Request({"type": "http", "method": "GET", "headers": []})
    with (
        patch.dict(
            "os.environ",
            {**_POSTGRES_ENV, "CLICKHOUSE_ENABLED": "false", "CHDB_ENABLED": "false"},
            clear=False,
        ),
        patch.object(mcp_server._postgres_backend, "psycopg", object()),
    ):
        response = await mcp_server.health_check(request)

    assert response.status_code == 200
    assert response.body == b"OK"


@pytest.mark.asyncio
async def test_health_check_hides_postgres_init_error_details():
    request = Request({"type": "http", "method": "GET", "headers": []})
    with (
        patch.dict(
            "os.environ",
            {**_POSTGRES_ENV, "CLICKHOUSE_ENABLED": "false", "CHDB_ENABLED": "false"},
            clear=False,
        ),
        patch.object(mcp_server._postgres_backend, "psycopg", None),
        patch.object(
            mcp_server._postgres_backend,
            "error_message",
            "Failed to initialize the Postgres driver: /opt/private/libpq.so",
        ),
    ):
        response = await mcp_server.health_check(request)

    assert response.status_code == 503
    assert b"Postgres initialization failed" in response.body
    assert b"/opt/private" not in response.body


@pytest.mark.asyncio
async def test_health_check_without_any_backend_is_misconfigured():
    request = Request({"type": "http", "method": "GET", "headers": []})
    with patch.dict(
        "os.environ",
        {"CLICKHOUSE_ENABLED": "false", "CHDB_ENABLED": "false", "POSTGRES_ENABLED": "false"},
        clear=False,
    ):
        response = await mcp_server.health_check(request)

    assert response.status_code == 503
    assert b"misconfigured" in response.body


def test_clickhouse_only_startup_does_not_import_psycopg():
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("POSTGRES_", "CHDB_"))
    }
    env.update({"CLICKHOUSE_ENABLED": "false", "MCP_CLICKHOUSE_TRUSTSTORE_DISABLE": "1"})
    script = "import sys, mcp_clickhouse; assert 'psycopg' not in sys.modules, 'psycopg imported'"

    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30
    )

    assert result.returncode == 0, result.stdout + result.stderr
