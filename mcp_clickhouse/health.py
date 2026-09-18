import asyncio
import concurrent.futures
import logging
import os
import threading
import time
import weakref
from typing import Optional, Tuple

from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_clickhouse import clients
from mcp_clickhouse.chdb_backend import _ChDBBackend
from mcp_clickhouse.executors import _Executors
from mcp_clickhouse.mcp_env import get_chdb_config

logger = logging.getLogger("mcp-clickhouse")

_HEALTH_CHECK_TIMEOUT_SECONDS = 2.0
_HEALTH_RESULT_CACHE_SECONDS = 1.0
_CLICKHOUSE_HEALTH_ERROR_BODY = (
    "ERROR. ClickHouse connection failed. Check server logs for details."
)


def _bounded_health_config(config: dict) -> dict:
    """Cap ClickHouse network timeouts to the public health timeout."""
    bounded = clients._ResolvedClientConfig(
        dict(config),
        overrides_applied=getattr(config, "overrides_applied", False),
    )
    for key in ("connect_timeout", "send_receive_timeout"):
        value = bounded.get(key)
        if value is None or value > _HEALTH_CHECK_TIMEOUT_SECONDS:
            bounded[key] = _HEALTH_CHECK_TIMEOUT_SECONDS
    return bounded


def _retrieve_health_probe_wrapper_result(future: asyncio.Future) -> None:
    """Retrieve a completed health wrapper result."""
    try:
        future.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


class _Health:
    """Keep health probes and cached results local to one server assembly."""

    chdb_backend: _ChDBBackend

    def __init__(self, executors: _Executors, clickhouse_clients: clients._ClickHouseClients):
        self.executors = executors
        self.clients = clickhouse_clients
        self.health_probe_future: Optional[concurrent.futures.Future] = None
        self.health_probe_lock = threading.Lock()
        self.logged_health_probe_futures: weakref.WeakSet[concurrent.futures.Future] = weakref.WeakSet()
        self.health_result_cache: Optional[Tuple[float, bool]] = None

    def _probe_clickhouse_health(self, config: dict) -> None:
        """Run an authenticated ClickHouse health query with a leased client."""
        entry = self.clients._acquire_clickhouse_client(config)
        try:
            entry.client.command("SELECT 1")
        finally:
            self.clients._release_client_entry(entry)

    def _clear_completed_health_probe(self, future: concurrent.futures.Future) -> None:
        """Clear the shared health future when its probe finishes."""
        with self.health_probe_lock:
            if self.health_probe_future is future:
                self.health_probe_future = None

    def _cache_health_probe_result(self, future: concurrent.futures.Future) -> None:
        """Cache a completed probe outcome for the reuse window."""
        if future.cancelled():
            return
        healthy = future.exception() is None
        with self.health_probe_lock:
            self.health_result_cache = (
                time.monotonic() + _HEALTH_RESULT_CACHE_SECONDS,
                healthy,
            )

    def _cached_health_result(self) -> Optional[bool]:
        """Return a cached probe outcome, or None when none is still valid."""
        with self.health_probe_lock:
            if self.health_result_cache is None:
                return None
            expires_at, healthy = self.health_result_cache
            if time.monotonic() >= expires_at:
                self.health_result_cache = None
                return None
            return healthy

    def _clear_health_result_cache(self) -> None:
        """Drop any cached probe outcome so the next check probes ClickHouse."""
        with self.health_probe_lock:
            self.health_result_cache = None

    def _get_health_probe_future(self, config: dict) -> concurrent.futures.Future:
        """Return the single in-flight ClickHouse health probe."""
        with self.health_probe_lock:
            if self.health_probe_future is not None and not self.health_probe_future.done():
                return self.health_probe_future
            future = self.executors.health.submit(
                self._probe_clickhouse_health,
                _bounded_health_config(config),
            )
            self.health_probe_future = future
        future.add_done_callback(self._cache_health_probe_result)
        future.add_done_callback(self._clear_completed_health_probe)
        return future

    def _claim_health_probe_log(self, future: Optional[concurrent.futures.Future]) -> bool:
        """Return true once for each shared health probe future."""
        if future is None:
            return True
        with self.health_probe_lock:
            if future in self.logged_health_probe_futures:
                return False
            self.logged_health_probe_futures.add(future)
            return True

    async def health_check(self, request: Request) -> PlainTextResponse:
        """Liveness probe. Intentionally unauthenticated and minimal.

        A completed ClickHouse probe result is reused for one second, so a failure
        or a recovery can be reported up to a second late.

        Debug via server logs.
        """
        future = None
        try:
            # Check if ClickHouse is enabled by trying to create config
            # If ClickHouse is disabled, this will succeed but connection will fail
            clickhouse_enabled = os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true"

            if not clickhouse_enabled:
                # If ClickHouse is disabled, check chDB status
                chdb_config = get_chdb_config()
                if chdb_config.enabled and self.chdb_backend.client is not None:
                    return PlainTextResponse("OK")
                elif chdb_config.enabled and self.chdb_backend.error_message:
                    return PlainTextResponse(
                        "ERROR. chDB initialization failed. Check server logs for details.",
                        status_code=503,
                    )
                else:
                    logger.error(
                        "Health check failed: both CLICKHOUSE_ENABLED=false and CHDB_ENABLED=false"
                    )
                    return PlainTextResponse(
                        "ERROR. Server misconfigured. Check server logs for details.",
                        status_code=503,
                    )

            cached_result = self._cached_health_result()
            if cached_result is not None:
                if cached_result:
                    return PlainTextResponse("OK")
                return PlainTextResponse(_CLICKHOUSE_HEALTH_ERROR_BODY, status_code=503)

            future = self._get_health_probe_future(clients._resolve_client_config())
            wrapped_future = asyncio.wrap_future(future)
            wrapped_future.add_done_callback(_retrieve_health_probe_wrapper_result)
            await asyncio.wait_for(
                asyncio.shield(wrapped_future),
                timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
            )
            return PlainTextResponse("OK")
        except asyncio.TimeoutError:
            if self._claim_health_probe_log(future):
                logger.warning(
                    "Health check timed out after %.1f seconds",
                    _HEALTH_CHECK_TIMEOUT_SECONDS,
                )
            return PlainTextResponse(_CLICKHOUSE_HEALTH_ERROR_BODY, status_code=503)
        except Exception:
            # Log the underlying error server-side, but don't leak details over the wire.
            if self._claim_health_probe_log(future):
                logger.exception("Health check failed: ClickHouse connection error")
            return PlainTextResponse(_CLICKHOUSE_HEALTH_ERROR_BODY, status_code=503)
