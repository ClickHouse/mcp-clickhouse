import asyncio
import atexit
import concurrent.futures
import logging
import os
import re
import threading
import time
import uuid
import weakref
from dataclasses import asdict, dataclass, field
from functools import wraps
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Annotated, Any, Dict, List, Optional, Tuple

from cachetools import TTLCache
from clickhouse_connect.driver.binding import external_bind_re, format_query_value
from fastmcp.exceptions import ToolError
from fastmcp.prompts import Prompt
from fastmcp.tools import Tool
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_clickhouse import clients
from mcp_clickhouse.auth import _initialize_auth_parser, _load_default_dotenv
from mcp_clickhouse.chdb_backend import _ChDBBackend, chdb_initial_prompt as chdb_initial_prompt
from mcp_clickhouse.clients import CLIENT_CONFIG_OVERRIDES_KEY as CLIENT_CONFIG_OVERRIDES_KEY
from mcp_clickhouse.executors import _Executors
from mcp_clickhouse.mcp_env import (
    get_chdb_config,
    get_config,
    get_mcp_config,
)
from mcp_clickhouse.serialization import _serialize_tool_result
from mcp_clickhouse.skills_advisor import CLICKHOUSE_SERVER_INSTRUCTIONS
from mcp_clickhouse.transport import ClickHouseFastMCP as ClickHouseFastMCP


@dataclass
class Column:
    database: str
    table: str
    name: str
    column_type: str
    default_kind: Optional[str]
    default_expression: Optional[str]
    comment: Optional[str]


@dataclass
class Table:
    database: str
    name: str
    engine: str
    create_table_query: str
    dependencies_database: str
    dependencies_table: str
    engine_full: str
    sorting_key: str
    primary_key: str
    total_rows: int
    total_bytes: int
    total_bytes_uncompressed: int
    parts: int
    active_parts: int
    total_marks: int
    comment: Optional[str] = None
    columns: List[Column] = field(default_factory=list)


@dataclass
class _ActiveQueryState:
    query: str
    client_entry: Optional[clients._ClientCacheEntry] = None
    cancelled: bool = False


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
_QUERY_CANCELLATION_WAIT_SECONDS = 1.0
_HEALTH_CHECK_TIMEOUT_SECONDS = 2.0
_HEALTH_RESULT_CACHE_SECONDS = 1.0
_CLICKHOUSE_HEALTH_ERROR_BODY = (
    "ERROR. ClickHouse connection failed. Check server logs for details."
)

_clickhouse_clients = clients._ClickHouseClients()

_active_queries: Dict[str, _ActiveQueryState] = {}
_active_queries_lock = threading.Lock()

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


def result_to_table(query_columns, result) -> List[Table]:
    return [Table(**dict(zip(query_columns, row))) for row in result]


def result_to_column(query_columns, result) -> List[Column]:
    return [Column(**dict(zip(query_columns, row))) for row in result]


def list_databases() -> str:
    """List available ClickHouse databases"""
    return _list_databases_with_config(clients._resolve_client_config())


def _list_databases_with_config(config: dict[str, Any]) -> str:
    """List databases with a resolved client configuration."""
    logger.info("Listing all databases")

    for attempt in range(2):
        entry = None
        try:
            entry = _clickhouse_clients._acquire_clickhouse_client(config)
            client = entry.client
            result = client.command("SHOW DATABASES")
            break
        except Exception as err:
            if attempt == 0 and clients._is_connection_error(err):
                logger.warning("list_databases connection error, retrying: %s", err)
                if entry is not None:
                    _clickhouse_clients._evict_cached_client(config, entry.client)
                continue
            raise
        finally:
            if entry is not None:
                _clickhouse_clients._release_client_entry(entry)

    # Convert newline-separated string to list and trim whitespace
    if isinstance(result, str):
        databases = [db.strip() for db in result.strip().split("\n")]
    else:
        databases = [result]

    logger.info(f"Found {len(databases)} databases")
    return _serialize_tool_result(databases)


@wraps(list_databases)
async def list_databases_async() -> str:
    overrides = await clients._get_client_config_overrides_for_tool()
    config = clients._resolve_client_config(overrides)
    future = _executors.metadata.submit(_list_databases_with_config, config)
    return await asyncio.wrap_future(future)


# Store pagination state for list_tables with an absolute 1-hour expiry.
table_pagination_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)  # 3600 seconds = 1 hour
_table_pagination_cache_lock = threading.Lock()
_PAGE_TOKEN_EXPIRES_AT = "_expires_at"
_CLAIM_PAGE_TOKEN_IN_WORKER = object()


@dataclass(frozen=True)
class _PendingPageToken:
    token: str
    state: dict[str, Any]


@dataclass(frozen=True)
class _PreparedListTablesResult:
    response: str
    pending_page_token: Optional[_PendingPageToken]


def fetch_table_names_from_system(
    client,
    database: str,
    like: Optional[str] = None,
    not_like: Optional[str] = None,
) -> List[str]:
    """Get list of table names from system.tables.

    Args:
        client: ClickHouse client
        database: Database name
        like: Optional pattern to filter table names (LIKE)
        not_like: Optional pattern to filter out table names (NOT LIKE)

    Returns:
        List of table names
    """
    query = f"SELECT name FROM system.tables WHERE database = {format_query_value(database)}"
    if like:
        query += f" AND name LIKE {format_query_value(like)}"

    if not_like:
        query += f" AND name NOT LIKE {format_query_value(not_like)}"

    result = client.query(query)
    table_names = [row[0] for row in result.result_rows]
    return table_names


def get_paginated_table_data(
    client,
    database: str,
    table_names: List[str],
    start_idx: int,
    page_size: int,
    include_detailed_columns: bool = True,
) -> tuple[List[Table], int, bool]:
    """Get detailed information for a page of tables.

    Args:
        client: ClickHouse client
        database: Database name
        table_names: List of all table names to paginate
        start_idx: Starting index for pagination
        page_size: Number of tables per page
        include_detailed_columns: Whether to include detailed column metadata (default: True)

    Returns:
        Tuple of (list of Table objects, end index, has more pages)
    """
    end_idx = min(start_idx + page_size, len(table_names))
    current_page_table_names = table_names[start_idx:end_idx]

    if not current_page_table_names:
        return [], end_idx, False

    query = f"""
        SELECT database, name, engine, create_table_query, dependencies_database,
               dependencies_table, engine_full, sorting_key, primary_key, total_rows,
               total_bytes, total_bytes_uncompressed, parts, active_parts, total_marks, comment
        FROM system.tables
        WHERE database = {format_query_value(database)}
        AND name IN ({", ".join(format_query_value(name) for name in current_page_table_names)})
    """

    result = client.query(query)
    tables = result_to_table(result.column_names, result.result_rows)

    if include_detailed_columns:
        for table in tables:
            column_data_query = f"""
                SELECT database, table, name, type AS column_type, default_kind, default_expression, comment
                FROM system.columns
                WHERE database = {format_query_value(database)}
                AND table = {format_query_value(table.name)}
            """
            column_data_query_result = client.query(column_data_query)
            table.columns = result_to_column(
                column_data_query_result.column_names,
                column_data_query_result.result_rows,
            )
    else:
        for table in tables:
            table.columns = []

    return tables, end_idx, end_idx < len(table_names)


def create_page_token(
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    table_names: List[str],
    end_idx: int,
    include_detailed_columns: bool,
) -> str:
    """Create a new page token and store it in the cache.

    Args:
        database: Database name
        like: LIKE pattern used to filter tables
        not_like: NOT LIKE pattern used to filter tables
        table_names: List of all table names
        end_idx: Index to start from for the next page
        include_detailed_columns: Whether to include detailed column metadata

    Returns:
        New page token
    """
    pending_page_token = _prepare_page_token(
        database,
        like,
        not_like,
        table_names,
        end_idx,
        include_detailed_columns,
    )
    _commit_page_token(pending_page_token)
    return pending_page_token.token


def _prepare_page_token(
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    table_names: List[str],
    end_idx: int,
    include_detailed_columns: bool,
) -> _PendingPageToken:
    """Prepare a page token without making it available to clients."""
    return _PendingPageToken(
        token=str(uuid.uuid4()),
        state={
            "database": database,
            "like": like,
            "not_like": not_like,
            "table_names": table_names,
            "start_idx": end_idx,
            "include_detailed_columns": include_detailed_columns,
        },
    )


def _commit_page_token(pending_page_token: _PendingPageToken) -> None:
    """Make a prepared page token available for one hour."""
    with _table_pagination_cache_lock:
        expires_at = table_pagination_cache.timer() + table_pagination_cache.ttl
        state = dict(pending_page_token.state)
        state[_PAGE_TOKEN_EXPIRES_AT] = expires_at
        table_pagination_cache[pending_page_token.token] = state


def _claim_page_token_for_request(
    page_token: str,
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    include_detailed_columns: bool,
) -> Optional[dict[str, Any]]:
    """Claim a matching page token and leave a mismatched token untouched."""
    mismatch = False
    with _table_pagination_cache_lock:
        state = table_pagination_cache.get(page_token)
        if state is None:
            return None
        expires_at = state.get(_PAGE_TOKEN_EXPIRES_AT)
        if expires_at is not None and expires_at <= table_pagination_cache.timer():
            table_pagination_cache.pop(page_token, None)
            return None
        cached_include_detailed = state.get("include_detailed_columns", True)
        mismatch = (
            state["database"] != database
            or state["like"] != like
            or state["not_like"] != not_like
            or cached_include_detailed != include_detailed_columns
        )
        if not mismatch:
            return table_pagination_cache.pop(page_token)

    logger.warning(
        "Page token %s is for a different database, filter, or metadata setting. "
        "Ignoring token and starting from beginning.",
        page_token,
    )
    return None


def _restore_page_token(page_token: str, state: dict[str, Any]) -> None:
    """Restore a claimed pagination token unless another value already exists."""
    with _table_pagination_cache_lock:
        expires_at = state.get(_PAGE_TOKEN_EXPIRES_AT)
        if expires_at is not None and expires_at <= table_pagination_cache.timer():
            return
        table_pagination_cache.setdefault(page_token, state)


def list_tables(
    database: str,
    like: Optional[str] = None,
    not_like: Optional[str] = None,
    page_token: Optional[str] = None,
    page_size: Annotated[int, Field(gt=0)] = 50,
    include_detailed_columns: bool = True,
) -> str:
    """List available ClickHouse tables in a database, including schema, comment,
    row count, and column count.

    Integers outside [-9007199254740991, 9007199254740991] in table metadata are
    returned as decimal strings.
    Pagination tokens are single-use and retained for up to one hour.

    Args:
        database: The database to list tables from
        like: Optional LIKE pattern to filter table names
        not_like: Optional NOT LIKE pattern to exclude table names
        page_token: Single-use token from a previous call, retained for up to one hour
        page_size: Number of tables to return per page (default: 50, must be greater than 0)
        include_detailed_columns: Whether to include detailed column metadata (default: True).
            When False, the columns array will be empty but create_table_query still contains
            all column information. This reduces payload size for large schemas.

    Returns:
        A JSON-encoded string of an object containing:
        - tables: List of table information (as dictionaries)
        - next_page_token: Token for the next page, or None if no more pages
        - total_tables: Total number of tables matching the filters
    """
    if page_size <= 0:
        raise ToolError("page_size must be greater than 0")

    return _list_tables_with_config(
        clients._resolve_client_config(),
        database,
        like,
        not_like,
        page_token,
        page_size,
        include_detailed_columns,
    )


def _list_tables_with_config(
    config: dict[str, Any],
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    page_token: Optional[str],
    page_size: int,
    include_detailed_columns: bool,
    claimed_page_state: object = _CLAIM_PAGE_TOKEN_IN_WORKER,
) -> str | _PreparedListTablesResult:
    """List tables with a resolved client configuration."""
    if page_size <= 0:
        raise ToolError("page_size must be greater than 0")

    owns_page_token_transaction = claimed_page_state is _CLAIM_PAGE_TOKEN_IN_WORKER
    if owns_page_token_transaction:
        claimed_page_state = (
            _claim_page_token_for_request(
                page_token,
                database,
                like,
                not_like,
                include_detailed_columns,
            )
            if page_token
            else None
        )

    logger.info(
        "Listing tables in database '%s' with like=%s, not_like=%s, "
        "page_token=%s, page_size=%s, include_detailed_columns=%s",
        database,
        like,
        not_like,
        page_token,
        page_size,
        include_detailed_columns,
    )

    try:
        for attempt in range(2):
            entry = None
            try:
                entry = _clickhouse_clients._acquire_clickhouse_client(config)
                client = entry.client
                prepared_result = _list_tables_impl(
                    client,
                    database,
                    like,
                    not_like,
                    page_token,
                    page_size,
                    include_detailed_columns,
                    claimed_page_state,
                )
                break
            except Exception as err:
                if attempt == 0 and clients._is_connection_error(err):
                    logger.warning("list_tables connection error, retrying: %s", err)
                    if entry is not None:
                        _clickhouse_clients._evict_cached_client(config, entry.client)
                    continue
                raise
            finally:
                if entry is not None:
                    _clickhouse_clients._release_client_entry(entry)
    except BaseException:
        if owns_page_token_transaction and page_token and claimed_page_state is not None:
            _restore_page_token(page_token, claimed_page_state)
        raise

    try:
        if not owns_page_token_transaction:
            return prepared_result
        if prepared_result.pending_page_token is not None:
            _commit_page_token(prepared_result.pending_page_token)
        return prepared_result.response
    except BaseException:
        if owns_page_token_transaction and page_token and claimed_page_state is not None:
            _restore_page_token(page_token, claimed_page_state)
        raise


@wraps(list_tables)
async def list_tables_async(
    database: str,
    like: Optional[str] = None,
    not_like: Optional[str] = None,
    page_token: Optional[str] = None,
    page_size: Annotated[int, Field(gt=0)] = 50,
    include_detailed_columns: bool = True,
) -> str:
    overrides = await clients._get_client_config_overrides_for_tool()
    config = clients._resolve_client_config(overrides)
    claimed_page_state = (
        _claim_page_token_for_request(
            page_token,
            database,
            like,
            not_like,
            include_detailed_columns,
        )
        if page_token
        else None
    )
    completed = False
    try:
        future = _executors.metadata.submit(
            _list_tables_with_config,
            config,
            database,
            like,
            not_like,
            page_token,
            page_size,
            include_detailed_columns,
            claimed_page_state,
        )
        prepared_result = await asyncio.wrap_future(future)
        if isinstance(prepared_result, str):
            completed = True
            return prepared_result
        if prepared_result.pending_page_token is not None:
            _commit_page_token(prepared_result.pending_page_token)
        completed = True
        return prepared_result.response
    finally:
        if not completed and page_token and claimed_page_state is not None:
            _restore_page_token(page_token, claimed_page_state)


def _list_tables_impl(
    client,
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    page_token: Optional[str],
    page_size: int,
    include_detailed_columns: bool,
    claimed_page_state: object = None,
) -> _PreparedListTablesResult:
    """Inner implementation of list_tables, separated for retry logic."""
    if claimed_page_state is not None:
        table_names = claimed_page_state["table_names"]
        start_idx = claimed_page_state["start_idx"]

        tables, end_idx, has_more = get_paginated_table_data(
            client,
            database,
            table_names,
            start_idx,
            page_size,
            include_detailed_columns,
        )

        pending_page_token = None
        if has_more:
            pending_page_token = _prepare_page_token(
                database, like, not_like, table_names, end_idx, include_detailed_columns
            )
        next_page_token = pending_page_token.token if pending_page_token else None

        logger.info(
            "Returned page with %s tables (total: %s), next_page_token=%s",
            len(tables),
            len(table_names),
            next_page_token,
        )
        return _PreparedListTablesResult(
            response=_serialize_tool_result({
                "tables": [asdict(table) for table in tables],
                "next_page_token": next_page_token,
                "total_tables": len(table_names),
            }),
            pending_page_token=pending_page_token,
        )

    table_names = fetch_table_names_from_system(client, database, like, not_like)

    start_idx = 0
    tables, end_idx, has_more = get_paginated_table_data(
        client,
        database,
        table_names,
        start_idx,
        page_size,
        include_detailed_columns,
    )

    pending_page_token = None
    if has_more:
        pending_page_token = _prepare_page_token(
            database, like, not_like, table_names, end_idx, include_detailed_columns
        )
    next_page_token = pending_page_token.token if pending_page_token else None

    logger.info(
        "Found %s tables, returning %s with next_page_token=%s",
        len(table_names),
        len(tables),
        next_page_token,
    )

    return _PreparedListTablesResult(
        response=_serialize_tool_result({
            "tables": [asdict(table) for table in tables],
            "next_page_token": next_page_token,
            "total_tables": len(table_names),
        }),
        pending_page_token=pending_page_token,
    )


# SQL comments and quoted text, blanked out before destructive-keyword matching.
# A keyword inside a string literal must not trigger the guard, and a keyword
# placed after a comment marker must not slip past it.
_SQL_COMMENTS_AND_QUOTED_TEXT = re.compile(
    r"""
      '(?:\\.|''|[^'\\])*'              # string literal
    | "(?:\\.|""|[^"\\])*"              # double-quoted identifier
    | `(?:\\.|``|[^`\\])*`              # backtick-quoted identifier
    | \$(?P<tag>\w*)\$.*?\$(?P=tag)\$    # dollar-quoted string or heredoc
    | --[^\n]*                         # line comment
    | \#[^\n]*                         # line comment
    | /\*.*?\*/                        # block comment
    """,
    re.VERBOSE | re.DOTALL,
)

# Matched against the scrubbed statement. Bare DROP also covers the
# ALTER ... DROP PARTITION/PART/COLUMN clauses. TRUNCATE followed by an open
# parenthesis is the rounding function, as is replace() after OR. Bare DELETE
# and UPDATE cover both the lightweight and ALTER mutation forms. REPLACE
# TABLE/PARTITION and OR REPLACE overwrite existing data. Bare CLEAR is
# reversible, so only CLEAR COLUMN/INDEX/PROJECTION is flagged.
_DESTRUCTIVE_KEYWORDS = re.compile(
    r"""
      \bDROP\b
    | \bTRUNCATE\b(?!\s*\()
    | \bDELETE\b
    | \bUPDATE\b
    | \bREPLACE\s+(?:TABLE|PARTITION)\b
    | \bOR\s+REPLACE\b(?!\s*\()
    | \bCLEAR\s+(?:COLUMN|INDEX|PROJECTION)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# DETACH ... PERMANENTLY is matched as two independent searches. A single
# `DETACH .* PERMANENTLY` branch backtracks quadratically on crafted input,
# and the validator runs on an executor thread that cancel cannot stop.
# Plain DETACH is reversible via ATTACH and stays allowed.
_DETACH_KEYWORD = re.compile(r"\bDETACH\b", re.IGNORECASE)
_PERMANENTLY_KEYWORD = re.compile(r"\bPERMANENTLY\b", re.IGNORECASE)
# Only mask the parameter name. Types and surrounding SQL remain visible.
_SQL_PARAMETER_NAME = re.compile(r"\{\s*[A-Za-z_$][A-Za-z0-9_$]*\s*:")
_SQL_PARAMETER_PREFIX = re.compile(r"\{[\w$]+:")


def _has_unclosed_placeholder(query: str, max_scan: int = 1_000_000) -> bool:
    """True if unterminated {name: placeholder starts would backtrack too far.

    external_bind_re's [^}]+ scans to the end of the string for every driver-valid
    {name: that has no closing } after it, which is only possible after the last }.
    A few such starts (placeholder-like text in a trailing comment or literal) are one
    linear scan each and harmless; a flood is quadratic here and in the driver's own
    bind path. Bounding the total scanned length keeps both linear. Only meaningful
    when params are supplied.
    """
    length = len(query)
    scanned = 0
    for prefix in _SQL_PARAMETER_PREFIX.finditer(query, query.rfind("}") + 1):
        if external_bind_re.fullmatch(prefix.group() + "String}") is not None:
            scanned += length - prefix.start()
            if scanned > max_scan:
                return True
    return False


def _strip_comments_and_quoted_text(query: str) -> str:
    """Blank out comments and quoted text so keyword matching sees only SQL syntax.

    Each match is replaced by a single space to keep surrounding tokens separate.
    Unterminated literals and comments do not match and are left in place, which
    keeps the destructive-operation check on the conservative side.
    """
    return _SQL_COMMENTS_AND_QUOTED_TEXT.sub(" ", query)


def _validate_query_for_destructive_ops(query: str) -> None:
    """Reject destructive statements unless CLICKHOUSE_ALLOW_DROP is set.

    Args:
        query: The SQL query to validate

    Raises:
        ToolError: If the query contains a destructive statement and CLICKHOUSE_ALLOW_DROP is not set
    """
    config = get_config()

    # If writes are not enabled, skip this check (readonly mode will catch it anyway)
    if not config.allow_write_access:
        return

    # If DROP is explicitly allowed, no validation needed
    if config.allow_drop:
        return

    statement = _strip_comments_and_quoted_text(query)
    statement = _SQL_PARAMETER_NAME.sub(" ", statement)
    if _DESTRUCTIVE_KEYWORDS.search(statement) or (
        _DETACH_KEYWORD.search(statement) and _PERMANENTLY_KEYWORD.search(statement)
    ):
        raise ToolError(
            "Destructive operations are not allowed (DROP, TRUNCATE, DELETE, UPDATE, "
            "REPLACE TABLE/PARTITION, CREATE OR REPLACE, CLEAR COLUMN/INDEX/PROJECTION, "
            "DETACH PERMANENTLY). Set CLICKHOUSE_ALLOW_DROP=true to enable them. "
            "This gate is a best-effort accident guard, not a security boundary. "
            "Restrict the ClickHouse user's grants for real enforcement."
        )


def _register_active_query(query_id: str, query: str) -> _ActiveQueryState:
    """Register query state before its worker is submitted."""
    state = _ActiveQueryState(query=query)
    with _active_queries_lock:
        _active_queries[query_id] = state
    return state


def _remove_active_query(query_id: str, state: _ActiveQueryState) -> None:
    """Remove query state if it still belongs to this execution."""
    with _active_queries_lock:
        if _active_queries.get(query_id) is state:
            _active_queries.pop(query_id)


def _mark_active_query_cancelled(query_id: str) -> Optional[_ActiveQueryState]:
    """Mark an active query cancelled before any server-side KILL attempt."""
    with _active_queries_lock:
        state = _active_queries.get(query_id)
        if state is not None:
            state.cancelled = True
        return state


def execute_query(
    query: str,
    query_id: str,
    client_config: dict,
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """Execute a query in a worker thread with a pre-resolved client config."""
    with _active_queries_lock:
        state = _active_queries.get(query_id)
        if state is None:
            state = _ActiveQueryState(query=query)
            _active_queries[query_id] = state

    entry = None
    try:
        if params is not None and (
            not isinstance(params, dict) or any(not isinstance(key, str) for key in params)
        ):
            raise ToolError("params must be an object with string keys")
        if params and _has_unclosed_placeholder(query):
            raise ToolError(
                "Too many unterminated ClickHouse {name:Type} placeholder starts. "
                "Close the braces. Placeholder-like text in a comment or string "
                "literal counts toward this when params is set."
            )
        # Keep parameters out of the driver's client-side formatting and raw binary paths.
        if params and (
            external_bind_re.search(query) is None
            or any(len(key) > 1 and key.startswith("$") and key.endswith("$") for key in params)
        ):
            raise ToolError(
                "params requires ClickHouse {name:Type} placeholders with the opening brace, "
                "name, and colon adjacent, for example {id:UInt32}. Spaces within the type "
                "are allowed. Python percent "
                "formatting and $name$ raw binary parameters are not supported."
            )
        entry = _clickhouse_clients._acquire_clickhouse_client(client_config)
        client = entry.client
        with _active_queries_lock:
            if state.cancelled:
                raise ToolError("Query cancelled before execution")
            state.client_entry = entry

        _validate_query_for_destructive_ops(query)

        query_settings = clients.build_query_settings(client)
        query_settings["query_id"] = query_id
        with _active_queries_lock:
            if state.cancelled:
                raise ToolError("Query cancelled before execution")
        res = client.query(query, parameters=params, settings=query_settings)
        logger.info(f"Query {query_id} returned {len(res.result_rows)} rows")
        return _serialize_tool_result({"columns": res.column_names, "rows": res.result_rows})
    except ToolError:
        raise
    except Exception as err:
        # Do not retry queries because a write may already have succeeded.
        if entry is not None and clients._is_connection_error(err):
            _clickhouse_clients._evict_cached_client(client_config, client)
        logger.error(f"Error executing query {query_id}: {err}")
        raise ToolError(f"Query execution failed: {str(err)}")
    finally:
        _remove_active_query(query_id, state)
        if entry is not None:
            _clickhouse_clients._release_client_entry(entry)


def _cancel_query(query_id: str):
    """Issue KILL QUERY on the ClickHouse server for a timed-out query.

    Uses the same cached client that originated the query. Cancellation
    failures are logged without masking the original timeout.
    """
    state = _mark_active_query_cancelled(query_id)

    if state is None:
        logger.debug("Query %s already completed, nothing to cancel", query_id)
        return

    try:
        safe_id = str(uuid.UUID(query_id))
    except ValueError:
        logger.warning("Refusing to KILL QUERY with non-UUID query_id: %r", query_id)
        return

    client = None
    try:
        client_entry = state.client_entry
        client = _clickhouse_clients._retain_client_entry(client_entry)
        if client is None:
            logger.warning(
                "Query %s cancelled before client acquisition completed",
                safe_id,
            )
            return

        logger.info("Cancelling query %s via KILL QUERY", safe_id)
        client.command(
            f"KILL QUERY WHERE query_id = {format_query_value(safe_id)}"
        )
        logger.info("Successfully cancelled query %s", safe_id)
    except Exception as e:
        logger.warning("Failed to cancel query %s: %s", safe_id, e)
    finally:
        if client is not None:
            _clickhouse_clients._release_client_entry(client_entry)


def _cancel_query_with_bounded_wait(query_id: str) -> None:
    """Run cancellation in its executor and wait briefly for completion."""
    future = _executors.cancellation.submit(_cancel_query, query_id)
    try:
        future.result(timeout=_QUERY_CANCELLATION_WAIT_SECONDS)
    except concurrent.futures.TimeoutError:
        logger.warning(
            "Cancellation for query %s exceeded %.1f seconds",
            query_id,
            _QUERY_CANCELLATION_WAIT_SECONDS,
        )


async def _cancel_query_async(query_id: str) -> None:
    """Await cancellation briefly without blocking the event loop."""
    future = _executors.cancellation.submit(_cancel_query, query_id)
    try:
        await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(future)),
            timeout=_QUERY_CANCELLATION_WAIT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Cancellation for query %s exceeded %.1f seconds",
            query_id,
            _QUERY_CANCELLATION_WAIT_SECONDS,
        )


def run_query(query: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Execute a SQL query against ClickHouse.

    Queries run in read-only mode by default. Set CLICKHOUSE_ALLOW_WRITE_ACCESS=true
    to allow DDL and DML statements when your ClickHouse server permits them.

    Bind params by name with ClickHouse placeholders such as {name:String} or
    {vector:Array(Float32)}. Values may be JSON scalars, nulls, or arrays. Pass exact
    large integers as decimal strings. JSON lists and objects cannot bind to Tuple
    and Map types. Python percent formatting and $name$ raw binary parameters are
    not supported. Parameter values stay out of the MCP server's normal SQL log lines,
    but may appear in errors and backend logs.
    """
    logger.info(f"Executing query: {query}")

    client_config = clients._resolve_client_config()
    query_id = str(uuid.uuid4())
    state = _register_active_query(query_id, query)

    try:
        with _active_queries_lock:
            in_flight = len(_active_queries)
        if in_flight >= _executors.max_workers:
            logger.warning(
                "Thread pool saturated: %d in-flight vs %d workers",
                in_flight, _executors.max_workers,
            )

        try:
            future = _executors.query.submit(execute_query, query, query_id, client_config, params)
        except Exception:
            _remove_active_query(query_id, state)
            raise
        timeout_secs = get_mcp_config().query_timeout
        try:
            return future.result(timeout=timeout_secs)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "Query %s timed out after %s seconds: %s", query_id, timeout_secs, query
            )
            if future.cancel():
                _remove_active_query(query_id, state)
            else:
                _mark_active_query_cancelled(query_id)
                _cancel_query_with_bounded_wait(query_id)
            raise ToolError(f"Query timed out after {timeout_secs} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error("Unexpected error in run_query: %s", str(e))
        raise RuntimeError(f"Unexpected error during query execution: {str(e)}")


async def run_query_async(query: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Async MCP-facing wrapper for ClickHouse queries.

    Awaits the worker-pool future asynchronously so concurrent tool calls are
    served while a slow query is in flight. Bind params by name with {name:Type}
    placeholders. Values may be JSON scalars, nulls, or arrays. JSON lists and
    objects cannot bind to Tuple and Map types. Python percent
    formatting and $name$ raw binary parameters are not supported.
    """
    logger.info(f"Executing query: {query}")

    overrides = await clients._get_client_config_overrides_for_tool()
    client_config = clients._resolve_client_config(overrides)
    query_id = str(uuid.uuid4())
    state = _register_active_query(query_id, query)

    try:
        with _active_queries_lock:
            in_flight = len(_active_queries)
        if in_flight >= _executors.max_workers:
            logger.warning(
                "Thread pool saturated: %d in-flight vs %d workers",
                in_flight, _executors.max_workers,
            )

        try:
            future = _executors.query.submit(execute_query, query, query_id, client_config, params)
        except Exception:
            _remove_active_query(query_id, state)
            raise
        timeout_secs = get_mcp_config().query_timeout
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=timeout_secs
            )
        except asyncio.CancelledError:
            if future.cancel():
                _remove_active_query(query_id, state)
            else:
                _mark_active_query_cancelled(query_id)
                await _cancel_query_async(query_id)
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "Query %s timed out after %s seconds: %s", query_id, timeout_secs, query
            )
            if future.cancel():
                _remove_active_query(query_id, state)
            else:
                _mark_active_query_cancelled(query_id)
                await _cancel_query_async(query_id)
            raise ToolError(f"Query timed out after {timeout_secs} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error("Unexpected error in run_query_async: %s", str(e))
        raise RuntimeError(f"Unexpected error during query execution: {str(e)}")


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
