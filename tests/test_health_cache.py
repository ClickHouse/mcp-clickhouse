"""Coverage for the short-lived cache in front of the ClickHouse health probe."""

import asyncio
import logging
import threading
from unittest.mock import patch

import pytest
from starlette.requests import Request

from mcp_clickhouse import mcp_server

HEALTH_ERROR_BODY = b"ERROR. ClickHouse connection failed. Check server logs for details."


def _health_request() -> Request:
    return Request({"type": "http", "method": "GET", "headers": []})


async def _wait_for_idle_probe() -> None:
    """Wait until the shared probe future is cleared so counts are stable."""
    for _ in range(100):
        with mcp_server._health_probe_lock:
            if mcp_server._health_probe_future is None:
                return
        await asyncio.sleep(0.01)
    raise AssertionError("health probe future was never cleared")


class _CountingProbe:
    """Record probe calls and optionally fail the first ones."""

    def __init__(self, failures: int = 0):
        self._lock = threading.Lock()
        self._failures = failures
        self.calls = 0

    def __call__(self, _config):
        with self._lock:
            self.calls += 1
            should_fail = self._failures > 0
            if should_fail:
                self._failures -= 1
        if should_fail:
            raise ConnectionError("password=secret backend down")


@pytest.mark.asyncio
async def test_successful_result_is_reused_within_the_cache_window():
    probe = _CountingProbe()

    with (
        patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_resolve_client_config", return_value={}),
        patch.object(mcp_server, "_probe_clickhouse_health", side_effect=probe),
    ):
        first = await mcp_server.health_check(_health_request())
        await _wait_for_idle_probe()
        second = await mcp_server.health_check(_health_request())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.body == b"OK"
    assert second.body == b"OK"
    assert probe.calls == 1


@pytest.mark.asyncio
async def test_failed_result_is_reused_within_the_cache_window(caplog):
    probe = _CountingProbe(failures=2)

    with caplog.at_level(logging.ERROR, logger="mcp-clickhouse"):
        with (
            patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
            patch.object(mcp_server, "_resolve_client_config", return_value={}),
            patch.object(mcp_server, "_probe_clickhouse_health", side_effect=probe),
        ):
            first = await mcp_server.health_check(_health_request())
            await _wait_for_idle_probe()
            second = await mcp_server.health_check(_health_request())

    assert first.status_code == 503
    assert second.status_code == 503
    assert first.body == HEALTH_ERROR_BODY
    assert second.body == HEALTH_ERROR_BODY
    assert probe.calls == 1

    failure_records = [
        record
        for record in caplog.records
        if "Health check failed: ClickHouse connection error" in record.message
    ]
    assert len(failure_records) == 1
    assert b"secret" not in second.body


@pytest.mark.asyncio
async def test_probe_runs_again_once_the_cache_window_expires():
    probe = _CountingProbe()

    with (
        patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_resolve_client_config", return_value={}),
        patch.object(mcp_server, "_probe_clickhouse_health", side_effect=probe),
        patch.object(mcp_server, "_HEALTH_RESULT_CACHE_SECONDS", 0.05),
    ):
        await mcp_server.health_check(_health_request())
        await _wait_for_idle_probe()
        await asyncio.sleep(0.06)
        await mcp_server.health_check(_health_request())
        await _wait_for_idle_probe()

    assert probe.calls == 2


@pytest.mark.asyncio
async def test_recovery_is_reported_after_the_cache_window_expires():
    probe = _CountingProbe(failures=1)

    with (
        patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_resolve_client_config", return_value={}),
        patch.object(mcp_server, "_probe_clickhouse_health", side_effect=probe),
        patch.object(mcp_server, "_HEALTH_RESULT_CACHE_SECONDS", 0.05),
    ):
        failed = await mcp_server.health_check(_health_request())
        await _wait_for_idle_probe()
        still_failed = await mcp_server.health_check(_health_request())
        await asyncio.sleep(0.06)
        recovered = await mcp_server.health_check(_health_request())
        await _wait_for_idle_probe()

    assert failed.status_code == 503
    assert still_failed.status_code == 503
    assert recovered.status_code == 200
    assert probe.calls == 2


@pytest.mark.asyncio
async def test_concurrent_requests_after_a_cached_result_do_not_probe_again():
    probe = _CountingProbe()

    with (
        patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_resolve_client_config", return_value={}),
        patch.object(mcp_server, "_probe_clickhouse_health", side_effect=probe),
    ):
        await mcp_server.health_check(_health_request())
        await _wait_for_idle_probe()
        responses = await asyncio.gather(
            *(mcp_server.health_check(_health_request()) for _ in range(50))
        )

    assert probe.calls == 1
    assert all(response.status_code == 200 for response in responses)


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_probe_and_cache_its_result():
    started = threading.Event()
    release = threading.Event()
    probe = _CountingProbe()

    def slow_probe(config):
        started.set()
        release.wait(timeout=1)
        probe(config)

    with (
        patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_resolve_client_config", return_value={}),
        patch.object(mcp_server, "_probe_clickhouse_health", side_effect=slow_probe),
        patch.object(
            mcp_server._executors.health,
            "submit",
            wraps=mcp_server._executors.health.submit,
        ) as submit,
    ):
        tasks = [asyncio.create_task(mcp_server.health_check(_health_request())) for _ in range(20)]
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
        finally:
            release.set()
        in_flight_responses = await asyncio.gather(*tasks)
        await _wait_for_idle_probe()
        cached_response = await mcp_server.health_check(_health_request())

    assert probe.calls == 1
    assert submit.call_count == 1
    assert all(response.status_code == 200 for response in in_flight_responses)
    assert cached_response.status_code == 200


@pytest.mark.asyncio
async def test_timed_out_check_caches_nothing_until_the_probe_finishes():
    started = threading.Event()
    release = threading.Event()

    def hanging_probe(_config):
        started.set()
        release.wait(timeout=1)

    with (
        patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_resolve_client_config", return_value={}),
        patch.object(mcp_server, "_probe_clickhouse_health", side_effect=hanging_probe),
        patch.object(mcp_server, "_HEALTH_CHECK_TIMEOUT_SECONDS", 0.05),
    ):
        try:
            timed_out = await mcp_server.health_check(_health_request())
            assert timed_out.status_code == 503
            assert mcp_server._cached_health_result() is None
        finally:
            release.set()
        await _wait_for_idle_probe()
        assert mcp_server._cached_health_result() is True
