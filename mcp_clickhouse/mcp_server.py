import asyncio
import atexit
import concurrent.futures
import logging
import os
import threading
import time
import weakref
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Optional, Tuple

from fastmcp.prompts import Prompt
from fastmcp.tools import Tool
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_clickhouse import clients
from mcp_clickhouse.auth import _initialize_auth_parser, _load_default_dotenv
from mcp_clickhouse.chdb_backend import _ChDBBackend, chdb_initial_prompt as chdb_initial_prompt
from mcp_clickhouse.clients import CLIENT_CONFIG_OVERRIDES_KEY as CLIENT_CONFIG_OVERRIDES_KEY
from mcp_clickhouse.executors import _Executors
from mcp_clickhouse.mcp_env import (
    get_chdb_config,
    get_mcp_config,
)
from mcp_clickhouse.metadata import (
    _Metadata,
    fetch_table_names_from_system as fetch_table_names_from_system,
    get_paginated_table_data as get_paginated_table_data,
)
from mcp_clickhouse.queries import _Queries
from mcp_clickhouse.skills_advisor import CLICKHOUSE_SERVER_INSTRUCTIONS
from mcp_clickhouse.transport import ClickHouseFastMCP as ClickHouseFastMCP


def _get_mcp_server_version() -> str:
    """Return the installed package version or an explicit unknown marker."""
    try:
        return package_version("mcp-clickhouse")
    except PackageNotFoundError:
        return "unknown"


MCP_SERVER_NAME = "mcp-clickhouse"
MCP_SERVER_VERSION = _get_mcp_server_version()

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(MCP_SERVER_NAME)

_load_default_dotenv()

_executors = _Executors(get_mcp_config().max_workers)
_HEALTH_CHECK_TIMEOUT_SECONDS = 2.0
_HEALTH_RESULT_CACHE_SECONDS = 1.0
_CLICKHOUSE_HEALTH_ERROR_BODY = (
    "ERROR. ClickHouse connection failed. Check server logs for details."
)

_clickhouse_clients = clients._ClickHouseClients()

_queries = _Queries(_executors, _clickhouse_clients)

_health_probe_future: Optional[concurrent.futures.Future] = None
_health_probe_lock = threading.Lock()
_logged_health_probe_futures: weakref.WeakSet[concurrent.futures.Future] = weakref.WeakSet()
_health_result_cache: Optional[Tuple[float, bool]] = None

_initialize_auth_parser()

mcp = ClickHouseFastMCP(
    name=MCP_SERVER_NAME,
    version=MCP_SERVER_VERSION,
    instructions=CLICKHOUSE_SERVER_INSTRUCTIONS,
)
_chdb_backend = _ChDBBackend(_executors)


def _probe_clickhouse_health(config: dict) -> None:
    """Run an authenticated ClickHouse health query with a leased client."""
    entry = _clickhouse_clients._acquire_clickhouse_client(config)
    try:
        entry.client.command("SELECT 1")
    finally:
        _clickhouse_clients._release_client_entry(entry)


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


def _clear_completed_health_probe(future: concurrent.futures.Future) -> None:
    """Clear the shared health future when its probe finishes."""
    global _health_probe_future
    with _health_probe_lock:
        if _health_probe_future is future:
            _health_probe_future = None


def _cache_health_probe_result(future: concurrent.futures.Future) -> None:
    """Cache a completed probe outcome for the reuse window."""
    global _health_result_cache
    if future.cancelled():
        return
    healthy = future.exception() is None
    with _health_probe_lock:
        _health_result_cache = (
            time.monotonic() + _HEALTH_RESULT_CACHE_SECONDS,
            healthy,
        )


def _cached_health_result() -> Optional[bool]:
    """Return a cached probe outcome, or None when none is still valid."""
    global _health_result_cache
    with _health_probe_lock:
        if _health_result_cache is None:
            return None
        expires_at, healthy = _health_result_cache
        if time.monotonic() >= expires_at:
            _health_result_cache = None
            return None
        return healthy


def _clear_health_result_cache() -> None:
    """Drop any cached probe outcome so the next check probes ClickHouse."""
    global _health_result_cache
    with _health_probe_lock:
        _health_result_cache = None


def _get_health_probe_future(config: dict) -> concurrent.futures.Future:
    """Return the single in-flight ClickHouse health probe."""
    global _health_probe_future
    with _health_probe_lock:
        if _health_probe_future is not None and not _health_probe_future.done():
            return _health_probe_future
        future = _executors.health.submit(
            _probe_clickhouse_health,
            _bounded_health_config(config),
        )
        _health_probe_future = future
    future.add_done_callback(_cache_health_probe_result)
    future.add_done_callback(_clear_completed_health_probe)
    return future


def _claim_health_probe_log(future: Optional[concurrent.futures.Future]) -> bool:
    """Return true once for each shared health probe future."""
    if future is None:
        return True
    with _health_probe_lock:
        if future in _logged_health_probe_futures:
            return False
        _logged_health_probe_futures.add(future)
        return True


def _retrieve_health_probe_wrapper_result(future: asyncio.Future) -> None:
    """Retrieve a completed health wrapper result."""
    try:
        future.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
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
            if chdb_config.enabled and _chdb_backend.client is not None:
                return PlainTextResponse("OK")
            elif chdb_config.enabled and _chdb_backend.error_message:
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

        cached_result = _cached_health_result()
        if cached_result is not None:
            if cached_result:
                return PlainTextResponse("OK")
            return PlainTextResponse(_CLICKHOUSE_HEALTH_ERROR_BODY, status_code=503)

        future = _get_health_probe_future(clients._resolve_client_config())
        wrapped_future = asyncio.wrap_future(future)
        wrapped_future.add_done_callback(_retrieve_health_probe_wrapper_result)
        await asyncio.wait_for(
            asyncio.shield(wrapped_future),
            timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
        )
        return PlainTextResponse("OK")
    except asyncio.TimeoutError:
        if _claim_health_probe_log(future):
            logger.warning(
                "Health check timed out after %.1f seconds",
                _HEALTH_CHECK_TIMEOUT_SECONDS,
            )
        return PlainTextResponse(_CLICKHOUSE_HEALTH_ERROR_BODY, status_code=503)
    except Exception:
        # Log the underlying error server-side, but don't leak details over the wire.
        if _claim_health_probe_log(future):
            logger.exception("Health check failed: ClickHouse connection error")
        return PlainTextResponse(_CLICKHOUSE_HEALTH_ERROR_BODY, status_code=503)


_metadata = _Metadata(_executors, _clickhouse_clients)
list_databases = _metadata.list_databases
list_databases_async = _metadata.list_databases_async
table_pagination_cache = _metadata.table_pagination_cache
create_page_token = _metadata.create_page_token
list_tables = _metadata.list_tables
list_tables_async = _metadata.list_tables_async


run_query = _queries.run_query
run_query_async = _queries.run_query_async


create_clickhouse_client = _clickhouse_clients.create_clickhouse_client


def _shutdown():
    # Drain every worker before closing the clients they may hold.
    _executors.shutdown()
    _clickhouse_clients._clear_client_cache()


atexit.register(_shutdown)


create_chdb_client = _chdb_backend.create_chdb_client
run_chdb_select_query = _chdb_backend.run_chdb_select_query
run_chdb_select_query_async = _chdb_backend.run_chdb_select_query_async


def _register_chdb_tools():
    """Register chDB tools when the feature is enabled and available.

    Note: This function is not idempotent. Calling it multiple times will
    register duplicate tools. It is intended to be called once at module load.
    """
    if not get_chdb_config().enabled:
        return

    _chdb_backend.client = _chdb_backend._init_chdb_client()
    if _chdb_backend.client is None:
        logger.warning("chDB is enabled but unavailable; skipping chDB tool registration")
        return

    atexit.register(_chdb_backend.client.close)
    mcp.add_tool(
        Tool.from_function(
            run_chdb_select_query_async,
            name="run_chdb_select_query",
            description=(
                "Run SQL in chDB, an in-process ClickHouse engine. Integers outside "
                "[-9007199254740991, 9007199254740991] are returned as decimal strings."
            ),
        )
    )
    chdb_prompt = Prompt.from_function(
        chdb_initial_prompt,
        name="chdb_initial_prompt",
        description="This prompt helps users understand how to interact and perform common operations in chDB",
    )
    mcp.add_prompt(chdb_prompt)
    logger.info("chDB tools and prompts registered")


if os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true":
    mcp.add_tool(Tool.from_function(list_databases_async, name="list_databases"))
    mcp.add_tool(Tool.from_function(list_tables_async, name="list_tables"))
    mcp.add_tool(
        Tool.from_function(
            run_query_async,
            name="run_query",
            description=(
                "Execute SQL queries in ClickHouse. Queries run in read-only mode by default. "
                "Bind optional params by name with {name:Type} placeholders, such as "
                "{name:String} or {vector:Array(Float32)}. Values may be JSON scalars, nulls, "
                "or arrays. Pass exact large integers as decimal strings. JSON lists and "
                "objects cannot bind to Tuple and Map types. Python percent "
                "formatting and $name$ raw binary parameters are not supported. Parameter values "
                "stay out of the MCP server's normal SQL log lines, but may appear in errors "
                "and backend logs. "
                "Set CLICKHOUSE_ALLOW_WRITE_ACCESS=true to allow DDL and DML operations. "
                "Set CLICKHOUSE_ALLOW_DROP=true to additionally allow destructive operations "
                "(DROP, TRUNCATE, DELETE, UPDATE, REPLACE TABLE/PARTITION, CREATE OR REPLACE, "
                "CLEAR COLUMN/INDEX/PROJECTION, DETACH PERMANENTLY). That gate is a best-effort "
                "accident guard, not a security boundary. Integers outside "
                "[-9007199254740991, 9007199254740991] are returned as decimal strings. "
                "Two optional checks also run through this tool. Use DESCRIBE (<query>) when "
                "you need a query's output columns and types; it inspects the result schema "
                "and surfaces analysis errors such as an unknown column, but a query that "
                "describes cleanly can still fail at runtime. Consider EXPLAIN ESTIMATE "
                "<query> before a SELECT that could be expensive; it returns the estimated "
                "parts, rows and marks read from MergeTree family tables, which is not run "
                "time and not result size. Neither runs the query body, though analysis can "
                "execute scalar subqueries."
            ),
        )
    )
    logger.info("ClickHouse tools registered")


_register_chdb_tools()
