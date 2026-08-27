"""Bounded, coalesced execution of the public readiness check.

`/health` is deliberately public so orchestrator probes can reach it without
credentials, which also makes it the one route an unauthenticated caller can
drive. Running the ClickHouse check straight from the route hands that caller a
blocking call on the ASGI event loop, so this module puts the check on a worker
thread, allows only one to be in flight at a time, and answers repeat probes
from a short-lived cached result.

The single-flight rule is what bounds the work rather than merely reducing it:
concurrent probes join the check already running instead of queueing more of
them, so the number of ClickHouse connections a probe storm can open is one.
"""

import asyncio
import concurrent.futures
import logging
import time
from typing import Callable, Optional, Tuple

logger = logging.getLogger("mcp-clickhouse")


class HealthGate:
    """Run a blocking readiness check off the event loop, one at a time.

    `check` is a callable that returns normally when the backend is reachable
    and raises otherwise. Its return value is ignored.
    """

    def __init__(
        self,
        check: Callable[[], object],
        timeout: float,
        cache_ttl: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._check = check
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._clock = clock
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mcp-health"
        )
        self._in_flight: Optional[asyncio.Future] = None
        self._cached: Optional[Tuple[bool, float]] = None

    async def healthy(self) -> bool:
        """Report whether the backend is reachable, within the configured wait.

        Never raises: a probe that cannot get an answer in time is reported as
        unhealthy, which is the same thing an orchestrator would conclude from a
        request that hung.
        """
        cached = self._cached
        if cached is not None and self._clock() - cached[1] < self._cache_ttl:
            return cached[0]

        # No await between reading and assigning `_in_flight`, so two callers
        # arriving in the same tick cannot both start a check.
        in_flight = self._in_flight
        if in_flight is None or in_flight.done():
            in_flight = asyncio.ensure_future(self._run())
            self._in_flight = in_flight

        try:
            # Shielded so that one caller giving up does not cancel the check
            # the other callers are still waiting on.
            return await asyncio.wait_for(asyncio.shield(in_flight), self._timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Health check did not finish within %ss; reporting unhealthy", self._timeout
            )
            return False

    async def _run(self) -> bool:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._executor, self._check)
            healthy = True
        except Exception:
            # Logged in full server-side; the route never puts details on the wire.
            logger.exception("Health check failed")
            healthy = False

        self._cached = (healthy, self._clock())
        return healthy

    def shutdown(self) -> None:
        """Release the worker thread. Used at interpreter exit and in tests."""
        self._executor.shutdown(wait=False)
