import asyncio
import builtins
import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

from mcp_clickhouse import mcp_server


def test_init_chdb_client_surfaces_optional_dependency_message():
    real_import = builtins.__import__

    def raising_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "chdb.session":
            error = ModuleNotFoundError("No module named 'chdb'")
            error.name = "chdb"
            raise error
        return real_import(name, globals, locals, fromlist, level)

    with (
        patch.dict("os.environ", {"CHDB_ENABLED": "true"}, clear=False),
        patch("builtins.__import__", side_effect=raising_import),
    ):
        client = mcp_server._init_chdb_client()

    assert client is None
    assert "mcp-clickhouse[chdb]" in mcp_server._chdb_error_message


def test_init_chdb_client_treats_other_import_errors_as_init_failures():
    real_import = builtins.__import__

    def raising_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "chdb.session":
            raise ImportError("dlopen(/tmp/chdb.so) failed")
        return real_import(name, globals, locals, fromlist, level)

    with (
        patch.dict("os.environ", {"CHDB_ENABLED": "true"}, clear=False),
        patch("builtins.__import__", side_effect=raising_import),
    ):
        client = mcp_server._init_chdb_client()

    assert client is None
    assert "Failed to initialize chDB client" in mcp_server._chdb_error_message
    assert "mcp-clickhouse[chdb]" not in mcp_server._chdb_error_message


def test_create_chdb_client_surfaces_optional_dependency_message():
    with (
        patch.dict("os.environ", {"CHDB_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_chdb_client", None),
        patch.object(
            mcp_server,
            "_chdb_error_message",
            "chDB support requires the optional dependency. "
            "Install mcp-clickhouse[chdb] to enable chDB features.",
        ),
    ):
        with pytest.raises(RuntimeError, match=r"mcp-clickhouse\[chdb\]"):
            mcp_server.create_chdb_client()


def test_register_chdb_tools_skips_when_client_is_unavailable():
    with (
        patch.dict("os.environ", {"CHDB_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_init_chdb_client", return_value=None),
        patch.object(mcp_server, "_chdb_client", None),
        patch.object(mcp_server.mcp, "add_tool") as add_tool,
        patch.object(mcp_server.mcp, "add_prompt") as add_prompt,
    ):
        mcp_server._register_chdb_tools()

    add_tool.assert_not_called()
    add_prompt.assert_not_called()


def test_register_chdb_tools_registers_when_client_is_available():
    mock_client = MagicMock()
    with (
        patch.dict("os.environ", {"CHDB_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_init_chdb_client", return_value=mock_client),
        patch.object(mcp_server, "_chdb_client", None),
        patch.object(mcp_server.mcp, "add_tool") as add_tool,
        patch.object(mcp_server.mcp, "add_prompt") as add_prompt,
    ):
        mcp_server._register_chdb_tools()

    add_tool.assert_called_once()
    add_prompt.assert_called_once()


@pytest.mark.asyncio
async def test_health_check_hides_internal_chdb_init_error_details():
    request = Request({"type": "http", "method": "GET", "headers": []})

    with (
        patch.dict(
            "os.environ",
            {"CLICKHOUSE_ENABLED": "false", "CHDB_ENABLED": "true"},
            clear=False,
        ),
        patch.object(mcp_server, "_chdb_client", None),
        patch.object(
            mcp_server,
            "_chdb_error_message",
            "Failed to initialize chDB client: /tmp/private.db is unreadable",
        ),
    ):
        response = await mcp_server.health_check(request)

    assert response.status_code == 503
    body = response.body.lower()
    assert b"initialization failed" in body
    assert b"check server logs for details" in body
    assert b"/tmp/private.db" not in body


@pytest.mark.asyncio
async def test_health_check_hides_clickhouse_connection_error_details():
    """A 503 response from a connection failure does not include the exception's hostname or credentials."""
    request = Request({"type": "http", "method": "GET", "headers": []})

    def raise_with_secrets(_config):
        raise ConnectionError(
            "HTTPConnectionPool(host='internal-ch.prod.mycorp.local', port=8443): "
            "password=hunter2 failed"
        )

    with (
        patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_resolve_client_config", return_value={}),
        patch.object(mcp_server, "_probe_clickhouse_health", side_effect=raise_with_secrets),
    ):
        response = await mcp_server.health_check(request)

    assert response.status_code == 503
    body = response.body.lower()
    assert b"clickhouse connection failed" in body
    assert b"check server logs for details" in body
    assert b"internal-ch.prod.mycorp.local" not in body
    assert b"hunter2" not in body


@pytest.mark.asyncio
async def test_health_check_rejects_cached_client_with_invalid_credentials():
    request = Request({"type": "http", "method": "GET", "headers": []})
    config = {
        "host": "localhost",
        "username": "expired-user",
        "connect_timeout": 2.0,
        "send_receive_timeout": 2.0,
    }
    client = MagicMock()
    client.command.side_effect = ConnectionError("password=secret-token rejected")
    entry = mcp_server._ClientCacheEntry(client, time.time())
    cache_key = mcp_server._config_to_cache_key(config)

    mcp_server._clear_client_cache()
    with mcp_server._client_cache_lock:
        mcp_server._client_cache[cache_key] = entry
    try:
        with (
            patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
            patch.object(mcp_server, "_resolve_client_config", return_value=config),
        ):
            response = await mcp_server.health_check(request)
    finally:
        mcp_server._clear_client_cache()

    assert response.status_code == 503
    assert response.body == (
        b"ERROR. ClickHouse connection failed. Check server logs for details."
    )
    assert b"secret-token" not in response.body
    client.command.assert_called_once_with("SELECT 1")
    client.ping.assert_not_called()


@pytest.mark.asyncio
async def test_health_check_probe_is_off_loop_and_bounded(caplog):
    request = Request({"type": "http", "method": "GET", "headers": []})
    started = threading.Event()
    release = threading.Event()

    def slow_probe(_config):
        started.set()
        release.wait(timeout=0.5)

    with caplog.at_level(logging.WARNING, logger="mcp-clickhouse"):
        with (
            patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
            patch.object(mcp_server, "_resolve_client_config", return_value={}),
            patch.object(mcp_server, "_probe_clickhouse_health", side_effect=slow_probe),
            patch.object(mcp_server, "_HEALTH_CHECK_TIMEOUT_SECONDS", 0.08),
        ):
            started_at = time.monotonic()
            task = asyncio.create_task(mcp_server.health_check(request))
            try:
                await asyncio.sleep(0.04)
                assert time.monotonic() - started_at < 0.15
                assert started.is_set()
                assert not task.done()
                response = await asyncio.wait_for(task, timeout=0.2)
            finally:
                release.set()

    for _ in range(50):
        with mcp_server._health_probe_lock:
            if mcp_server._health_probe_future is None:
                break
        await asyncio.sleep(0.01)

    assert response.status_code == 503
    assert response.body == (
        b"ERROR. ClickHouse connection failed. Check server logs for details."
    )
    timeout_records = [
        record for record in caplog.records if "Health check timed out" in record.message
    ]
    assert len(timeout_records) == 1
    assert timeout_records[0].exc_info is None


@pytest.mark.asyncio
async def test_concurrent_health_checks_share_one_bounded_probe(caplog):
    request = Request({"type": "http", "method": "GET", "headers": []})
    started = threading.Event()
    release = threading.Event()
    received_configs = []

    def slow_probe(config):
        received_configs.append(config)
        started.set()
        release.wait(timeout=0.5)

    with caplog.at_level(logging.WARNING, logger="mcp-clickhouse"):
        with (
            patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
            patch.object(
                mcp_server,
                "_resolve_client_config",
                return_value={"connect_timeout": 30, "send_receive_timeout": 45},
            ),
            patch.object(mcp_server, "_probe_clickhouse_health", side_effect=slow_probe),
            patch.object(mcp_server, "_HEALTH_CHECK_TIMEOUT_SECONDS", 0.2),
            patch.object(
                mcp_server.HEALTH_EXECUTOR,
                "submit",
                wraps=mcp_server.HEALTH_EXECUTOR.submit,
            ) as submit,
        ):
            tasks = [
                asyncio.create_task(mcp_server.health_check(request)) for _ in range(200)
            ]
            try:
                for _ in range(20):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.02)

                assert started.is_set()
                assert submit.call_count == 1
                assert mcp_server.HEALTH_EXECUTOR._work_queue.qsize() == 0
                assert received_configs == [
                    {"connect_timeout": 0.2, "send_receive_timeout": 0.2}
                ]
                responses = await asyncio.gather(*tasks)
            finally:
                release.set()

    for _ in range(50):
        with mcp_server._health_probe_lock:
            if mcp_server._health_probe_future is None:
                break
        await asyncio.sleep(0.01)

    assert all(response.status_code == 503 for response in responses)
    assert all(
        response.body
        == b"ERROR. ClickHouse connection failed. Check server logs for details."
        for response in responses
    )
    timeout_records = [
        record for record in caplog.records if "Health check timed out" in record.message
    ]
    assert len(timeout_records) == 1
