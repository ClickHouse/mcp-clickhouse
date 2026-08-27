"""Tests for the bounded, coalesced public health check."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

from mcp_clickhouse import mcp_server
from mcp_clickhouse.health import HealthGate
from mcp_clickhouse.mcp_env import MCPServerConfig


class _FakeClock:
    """Monotonic clock the tests advance by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _gate(check, timeout=5.0, cache_ttl=0.0, clock=None):
    return HealthGate(check, timeout=timeout, cache_ttl=cache_ttl, clock=clock or _FakeClock())


class TestOffTheEventLoop:
    """The blocking check must not run on the ASGI event loop."""

    @pytest.mark.asyncio
    async def test_event_loop_keeps_running_during_a_slow_check(self):
        """A probe against an unresponsive backend cannot stall other requests."""
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.001)
                ticks += 1

        def slow_check():
            # Blocking, exactly like clickhouse-connect client construction.
            import time

            time.sleep(0.2)

        gate = _gate(slow_check)
        spinner = asyncio.ensure_future(ticker())
        try:
            assert await gate.healthy() is True
        finally:
            spinner.cancel()
            gate.shutdown()

        # Serialized on the loop this would be 0. The exact count varies by
        # machine, so only assert that the loop kept making progress.
        assert ticks > 10

    @pytest.mark.asyncio
    async def test_check_runs_on_a_worker_thread(self):
        import threading

        loop_thread = threading.get_ident()
        seen = {}

        def check():
            seen["thread"] = threading.get_ident()

        gate = _gate(check)
        try:
            await gate.healthy()
        finally:
            gate.shutdown()

        assert seen["thread"] != loop_thread


class TestCoalescing:
    """Concurrent probes join one check instead of starting more."""

    @pytest.mark.asyncio
    async def test_concurrent_probes_run_the_check_once(self):
        calls = 0
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def check():
            nonlocal calls
            calls += 1
            # Hold the check open until every caller is waiting on it.
            asyncio.run_coroutine_threadsafe(_wait(release), loop).result(timeout=2)

        async def _wait(event):
            await event.wait()

        gate = _gate(check)
        try:
            probes = [asyncio.ensure_future(gate.healthy()) for _ in range(20)]
            await asyncio.sleep(0.05)
            release.set()
            results = await asyncio.gather(*probes)
        finally:
            gate.shutdown()

        assert results == [True] * 20
        assert calls == 1

    @pytest.mark.asyncio
    async def test_a_later_probe_starts_a_new_check(self):
        """Coalescing only joins checks that are still running."""
        calls = 0

        def check():
            nonlocal calls
            calls += 1

        gate = _gate(check)
        try:
            assert await gate.healthy() is True
            assert await gate.healthy() is True
        finally:
            gate.shutdown()

        assert calls == 2


class TestCache:
    """A result stays reusable for the configured TTL and no longer."""

    @pytest.mark.asyncio
    async def test_result_is_reused_within_the_ttl(self):
        calls = 0
        clock = _FakeClock()

        def check():
            nonlocal calls
            calls += 1

        gate = _gate(check, cache_ttl=5.0, clock=clock)
        try:
            assert await gate.healthy() is True
            clock.advance(4.9)
            assert await gate.healthy() is True
        finally:
            gate.shutdown()

        assert calls == 1

    @pytest.mark.asyncio
    async def test_result_is_rechecked_after_the_ttl(self):
        calls = 0
        clock = _FakeClock()

        def check():
            nonlocal calls
            calls += 1

        gate = _gate(check, cache_ttl=5.0, clock=clock)
        try:
            assert await gate.healthy() is True
            clock.advance(5.1)
            assert await gate.healthy() is True
        finally:
            gate.shutdown()

        assert calls == 2

    @pytest.mark.asyncio
    async def test_a_failure_is_cached_too(self):
        """Otherwise an unreachable backend is reconnected to on every probe."""
        calls = 0
        clock = _FakeClock()

        def check():
            nonlocal calls
            calls += 1
            raise ConnectionError("unreachable")

        gate = _gate(check, cache_ttl=5.0, clock=clock)
        try:
            assert await gate.healthy() is False
            assert await gate.healthy() is False
        finally:
            gate.shutdown()

        assert calls == 1

    @pytest.mark.asyncio
    async def test_zero_ttl_checks_every_time(self):
        calls = 0

        def check():
            nonlocal calls
            calls += 1

        gate = _gate(check, cache_ttl=0.0)
        try:
            await gate.healthy()
            await gate.healthy()
        finally:
            gate.shutdown()

        assert calls == 2


class TestTimeout:
    """A check that does not finish in time answers rather than hanging."""

    @pytest.mark.asyncio
    async def test_slow_check_reports_unhealthy_within_the_bound(self):
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def check():
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)

        gate = _gate(check, timeout=0.05)
        try:
            assert await gate.healthy() is False
        finally:
            release.set()
            gate.shutdown()

    @pytest.mark.asyncio
    async def test_one_caller_timing_out_does_not_cancel_the_others(self):
        """The shared check is shielded, so a short waiter cannot abort it."""
        release = asyncio.Event()
        loop = asyncio.get_running_loop()
        finished = False

        def check():
            nonlocal finished
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
            finished = True

        gate = _gate(check, timeout=5.0)
        try:
            impatient = asyncio.ensure_future(
                asyncio.wait_for(asyncio.shield(gate.healthy()), 0.05)
            )
            patient = asyncio.ensure_future(gate.healthy())
            await asyncio.sleep(0.1)
            with pytest.raises(asyncio.TimeoutError):
                await impatient
            release.set()
            assert await patient is True
        finally:
            gate.shutdown()

        assert finished is True


class TestProbe:
    """_probe_clickhouse is a dedicated connection check, not the client factory."""

    def _config(self):
        return {
            "host": "ch.example.com",
            "port": 8443,
            "username": "u",
            "password": "p",
            "interface": "https",
            "secure": True,
            "verify": True,
            "connect_timeout": 30,
            "send_receive_timeout": 300,
            "client_name": "mcp_clickhouse",
        }

    def test_client_is_closed(self):
        """clickhouse-connect only unregisters a TLS pool manager on close()."""
        client = MagicMock()
        config = MagicMock()
        config.get_client_config.return_value = self._config()

        with (
            patch.object(mcp_server, "get_config", return_value=config),
            patch.object(mcp_server.clickhouse_connect, "get_client", return_value=client),
        ):
            mcp_server._probe_clickhouse()

        client.close.assert_called_once_with()

    def test_driver_timeouts_are_clamped_to_the_health_bound(self):
        client = MagicMock()
        config = MagicMock()
        config.get_client_config.return_value = self._config()

        with (
            patch.dict("os.environ", {"CLICKHOUSE_MCP_HEALTH_TIMEOUT": "2"}, clear=False),
            patch.object(mcp_server, "get_mcp_config", return_value=MCPServerConfig()),
            patch.object(mcp_server, "get_config", return_value=config),
            patch.object(
                mcp_server.clickhouse_connect, "get_client", return_value=client
            ) as get_client,
        ):
            mcp_server._probe_clickhouse()

        passed = get_client.call_args.kwargs
        assert passed["connect_timeout"] == 2
        assert passed["send_receive_timeout"] == 2

    def test_shorter_configured_timeouts_are_left_alone(self):
        """The bound is a ceiling, not an override."""
        client = MagicMock()
        config = MagicMock()
        client_config = self._config()
        client_config["connect_timeout"] = 1
        client_config["send_receive_timeout"] = 1
        config.get_client_config.return_value = client_config

        with (
            patch.dict("os.environ", {"CLICKHOUSE_MCP_HEALTH_TIMEOUT": "5"}, clear=False),
            patch.object(mcp_server, "get_mcp_config", return_value=MCPServerConfig()),
            patch.object(mcp_server, "get_config", return_value=config),
            patch.object(
                mcp_server.clickhouse_connect, "get_client", return_value=client
            ) as get_client,
        ):
            mcp_server._probe_clickhouse()

        passed = get_client.call_args.kwargs
        assert passed["connect_timeout"] == 1
        assert passed["send_receive_timeout"] == 1

    def test_probe_does_not_use_the_shared_client_factory(self):
        """The factory applies request overrides and consumes the one-shot advisory."""
        client = MagicMock()
        config = MagicMock()
        config.get_client_config.return_value = self._config()

        with (
            patch.object(mcp_server, "get_config", return_value=config),
            patch.object(mcp_server.clickhouse_connect, "get_client", return_value=client),
            patch.object(mcp_server, "create_clickhouse_client") as factory,
        ):
            mcp_server._probe_clickhouse()

        factory.assert_not_called()


class TestResponseBodies:
    """The 200/503 contract the orchestrator depends on is unchanged."""

    def _request(self):
        return Request({"type": "http", "method": "GET", "headers": []})

    @pytest.mark.asyncio
    async def test_healthy_returns_plain_ok(self):
        with (
            patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
            patch.object(mcp_server, "_health_gate", None),
            patch.object(mcp_server, "_probe_clickhouse", return_value=None),
        ):
            response = await mcp_server.health_check(self._request())

        assert response.status_code == 200
        assert response.body == b"OK"

    @pytest.mark.asyncio
    async def test_unhealthy_returns_generic_503(self):
        with (
            patch.dict("os.environ", {"CLICKHOUSE_ENABLED": "true"}, clear=False),
            patch.object(mcp_server, "_health_gate", None),
            patch.object(
                mcp_server, "_probe_clickhouse", side_effect=ConnectionError("boom")
            ),
        ):
            response = await mcp_server.health_check(self._request())

        assert response.status_code == 503
        assert response.body == (
            b"ERROR. ClickHouse connection failed. Check server logs for details."
        )

    @pytest.mark.asyncio
    async def test_timeout_does_not_leak_backend_details(self):
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def hang():
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)

        with (
            patch.dict(
                "os.environ",
                {"CLICKHOUSE_ENABLED": "true", "CLICKHOUSE_MCP_HEALTH_TIMEOUT": "0.05"},
                clear=False,
            ),
            patch.object(mcp_server, "_health_gate", None),
            patch.object(mcp_server, "get_mcp_config", return_value=MCPServerConfig()),
            patch.object(mcp_server, "_probe_clickhouse", side_effect=hang),
        ):
            response = await mcp_server.health_check(self._request())
            release.set()

        assert response.status_code == 503
        body = response.body.lower()
        assert b"check server logs for details" in body
        assert b"timeout" not in body
        assert b"clickhouse-connect" not in body


class TestConfig:
    """Health bounds are configurable and validated."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CLICKHOUSE_MCP_HEALTH_TIMEOUT", raising=False)
        monkeypatch.delenv("CLICKHOUSE_MCP_HEALTH_CACHE_TTL", raising=False)

        config = MCPServerConfig()

        assert config.health_timeout == 5
        assert config.health_cache_ttl == 5

    def test_timeout_must_be_positive(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLICKHOUSE_MCP_HEALTH_TIMEOUT", "0")

        with pytest.raises(ValueError, match="CLICKHOUSE_MCP_HEALTH_TIMEOUT"):
            MCPServerConfig().health_timeout

    def test_cache_ttl_may_be_zero_but_not_negative(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLICKHOUSE_MCP_HEALTH_CACHE_TTL", "0")
        assert MCPServerConfig().health_cache_ttl == 0

        monkeypatch.setenv("CLICKHOUSE_MCP_HEALTH_CACHE_TTL", "-1")
        with pytest.raises(ValueError, match="CLICKHOUSE_MCP_HEALTH_CACHE_TTL"):
            MCPServerConfig().health_cache_ttl
