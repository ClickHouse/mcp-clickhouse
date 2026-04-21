import atexit
import concurrent.futures
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Dict, List, Optional, Tuple

import clickhouse_connect
from cachetools import TTLCache
from clickhouse_connect.driver.binding import format_query_value
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.prompts import Prompt
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_context
from fastmcp.tools import Tool
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_clickhouse.chdb_prompt import CHDB_PROMPT
from mcp_clickhouse.mcp_env import TransportType, get_chdb_config, get_config, get_mcp_config


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


MCP_SERVER_NAME = "mcp-clickhouse"
CLIENT_CONFIG_OVERRIDES_KEY = "clickhouse_client_config_overrides"

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(MCP_SERVER_NAME)

load_dotenv()

_max_workers = get_mcp_config().max_workers
QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers)
atexit.register(lambda: QUERY_EXECUTOR.shutdown(wait=True))

# --- Client cache ---
# Cache of ClickHouse clients keyed by frozen config, enabling client reuse
# across tool calls. Each entry is (client, last_used_timestamp).
_client_cache: Dict[Tuple, Tuple] = {}
_client_cache_lock = threading.Lock()
_CLIENT_IDLE_PING_THRESHOLD = 60  # seconds before we ping to verify liveness

# --- Active query tracker ---
# Maps query_id -> (cache_key, query_text) so we can KILL QUERY on the
# correct server when a timeout fires.
_active_queries: Dict[str, Tuple] = {}
_active_queries_lock = threading.Lock()

# Configure authentication for HTTP/SSE transports
auth_provider = None
mcp_config = get_mcp_config()
http_transports = [TransportType.HTTP.value, TransportType.SSE.value]

if mcp_config.server_transport in http_transports:
    if mcp_config.auth_disabled:
        logger.warning("WARNING: MCP SERVER AUTHENTICATION IS DISABLED")
        logger.warning("Only use this for local development/testing.")
        logger.warning("DO NOT expose to networks.")
    elif mcp_config.auth_token:
        auth_provider = StaticTokenVerifier(
            tokens={mcp_config.auth_token: {"client_id": "mcp-client", "scopes": []}},
            required_scopes=[],
        )
        logger.info("Authentication enabled for HTTP/SSE transport")
    else:
        # No token configured and auth not disabled
        raise ValueError(
            "Authentication token required for HTTP/SSE transports. "
            "Set CLICKHOUSE_MCP_AUTH_TOKEN environment variable or set "
            "CLICKHOUSE_MCP_AUTH_DISABLED=true (for development only)."
        )

mcp = FastMCP(name=MCP_SERVER_NAME, auth=auth_provider)
_chdb_client = None
_chdb_error_message: Optional[str] = None


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    """Health check endpoint for monitoring server status.

    Returns OK if the server is running and can connect to ClickHouse.
    """
    if auth_provider is not None:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return PlainTextResponse("Unauthorized", status_code=401)

        token = auth_header[7:]
        access_token = await auth_provider.verify_token(token)
        if access_token is None:
            return PlainTextResponse("Unauthorized", status_code=401)

    try:
        # Check if ClickHouse is enabled by trying to create config
        # If ClickHouse is disabled, this will succeed but connection will fail
        clickhouse_enabled = os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true"

        if not clickhouse_enabled:
            # If ClickHouse is disabled, check chDB status
            chdb_config = get_chdb_config()
            if chdb_config.enabled and _chdb_client is not None:
                return PlainTextResponse("OK - MCP server running with chDB enabled")
            elif chdb_config.enabled and _chdb_error_message:
                return PlainTextResponse(
                    "ERROR. chDB initialization failed. Check server logs for details.",
                    status_code=503,
                )
            else:
                # Both ClickHouse and chDB are disabled - this is an error
                return PlainTextResponse(
                    "ERROR - Both ClickHouse and chDB are disabled. At least one must be enabled.",
                    status_code=503,
                )

        # Try to create a client connection to verify ClickHouse connectivity
        client = create_clickhouse_client()
        version = client.server_version
        return PlainTextResponse(f"OK - Connected to ClickHouse {version}")
    except Exception as e:
        # Return 503 Service Unavailable if we can't connect to ClickHouse
        return PlainTextResponse(f"ERROR - Cannot connect to ClickHouse: {str(e)}", status_code=503)


def result_to_table(query_columns, result) -> List[Table]:
    return [Table(**dict(zip(query_columns, row))) for row in result]


def result_to_column(query_columns, result) -> List[Column]:
    return [Column(**dict(zip(query_columns, row))) for row in result]


def to_json(obj: Any) -> str:
    if is_dataclass(obj):
        return json.dumps(asdict(obj), default=to_json)
    elif isinstance(obj, list):
        return [to_json(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: to_json(value) for key, value in obj.items()}
    return obj


def list_databases():
    """List available ClickHouse databases"""
    logger.info("Listing all databases")
    config = _resolve_client_config()

    for attempt in range(2):
        try:
            client = create_clickhouse_client(config=config)
            result = client.command("SHOW DATABASES")
            break
        except Exception as err:
            if attempt == 0 and _is_connection_error(err):
                logger.warning("list_databases connection error, retrying: %s", err)
                _evict_cached_client(config)
                continue
            raise

    # Convert newline-separated string to list and trim whitespace
    if isinstance(result, str):
        databases = [db.strip() for db in result.strip().split("\n")]
    else:
        databases = [result]

    logger.info(f"Found {len(databases)} databases")
    return json.dumps(databases)


# Store pagination state for list_tables with 1-hour expiry
# Using TTLCache from cachetools to automatically expire entries after 1 hour
table_pagination_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)  # 3600 seconds = 1 hour


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
    token = str(uuid.uuid4())
    table_pagination_cache[token] = {
        "database": database,
        "like": like,
        "not_like": not_like,
        "table_names": table_names,
        "start_idx": end_idx,
        "include_detailed_columns": include_detailed_columns,
    }
    return token


def list_tables(
    database: str,
    like: Optional[str] = None,
    not_like: Optional[str] = None,
    page_token: Optional[str] = None,
    page_size: int = 50,
    include_detailed_columns: bool = True,
) -> Dict[str, Any]:
    """List available ClickHouse tables in a database, including schema, comment,
    row count, and column count.

    Args:
        database: The database to list tables from
        like: Optional LIKE pattern to filter table names
        not_like: Optional NOT LIKE pattern to exclude table names
        page_token: Token for pagination, obtained from a previous call
        page_size: Number of tables to return per page (default: 50)
        include_detailed_columns: Whether to include detailed column metadata (default: True).
            When False, the columns array will be empty but create_table_query still contains
            all column information. This reduces payload size for large schemas.

    Returns:
        A dictionary containing:
        - tables: List of table information (as dictionaries)
        - next_page_token: Token for the next page, or None if no more pages
        - total_tables: Total number of tables matching the filters
    """
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
    config = _resolve_client_config()

    for attempt in range(2):
        try:
            client = create_clickhouse_client(config=config)
            return _list_tables_impl(
                client, database, like, not_like, page_token,
                page_size, include_detailed_columns,
            )
        except Exception as err:
            if attempt == 0 and _is_connection_error(err):
                logger.warning("list_tables connection error, retrying: %s", err)
                _evict_cached_client(config)
                continue
            raise


def _list_tables_impl(
    client,
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    page_token: Optional[str],
    page_size: int,
    include_detailed_columns: bool,
) -> Dict[str, Any]:
    """Inner implementation of list_tables, separated for retry logic."""
    if page_token and page_token in table_pagination_cache:
        cached_state = table_pagination_cache[page_token]
        cached_include_detailed = cached_state.get("include_detailed_columns", True)

        if (
            cached_state["database"] != database
            or cached_state["like"] != like
            or cached_state["not_like"] != not_like
            or cached_include_detailed != include_detailed_columns
        ):
            logger.warning(
                "Page token %s is for a different database, filter, or metadata setting. "
                "Ignoring token and starting from beginning.",
                page_token,
            )
            page_token = None
        else:
            table_names = cached_state["table_names"]
            start_idx = cached_state["start_idx"]

            tables, end_idx, has_more = get_paginated_table_data(
                client,
                database,
                table_names,
                start_idx,
                page_size,
                include_detailed_columns,
            )

            next_page_token = None
            if has_more:
                next_page_token = create_page_token(
                    database, like, not_like, table_names, end_idx, include_detailed_columns
                )

            del table_pagination_cache[page_token]

            logger.info(
                "Returned page with %s tables (total: %s), next_page_token=%s",
                len(tables),
                len(table_names),
                next_page_token,
            )
            return {
                "tables": [asdict(table) for table in tables],
                "next_page_token": next_page_token,
                "total_tables": len(table_names),
            }

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

    next_page_token = None
    if has_more:
        next_page_token = create_page_token(
            database, like, not_like, table_names, end_idx, include_detailed_columns
        )

    logger.info(
        "Found %s tables, returning %s with next_page_token=%s",
        len(table_names),
        len(tables),
        next_page_token,
    )

    return {
        "tables": [asdict(table) for table in tables],
        "next_page_token": next_page_token,
        "total_tables": len(table_names),
    }


def _validate_query_for_destructive_ops(query: str) -> None:
    """Validate that destructive operations (DROP, TRUNCATE) are allowed.

    Args:
        query: The SQL query to validate

    Raises:
        ToolError: If the query contains destructive operations but CLICKHOUSE_ALLOW_DROP is not set
    """
    config = get_config()

    # If writes are not enabled, skip this check (readonly mode will catch it anyway)
    if not config.allow_write_access:
        return

    # If DROP is explicitly allowed, no validation needed
    if config.allow_drop:
        return

    # Simple pattern matching for destructive operations
    destructive_pattern = r"\b(DROP\s+(\S+\s+)*(TABLE|DATABASE|VIEW|DICTIONARY)|TRUNCATE\s+TABLE)\b"
    if re.search(destructive_pattern, query, re.IGNORECASE):
        raise ToolError(
            "Destructive operations (DROP, TRUNCATE) are not allowed. "
            "Set CLICKHOUSE_ALLOW_DROP=true to enable these operations. "
            "This is a safety feature to prevent accidental data deletion."
        )


def _is_connection_error(err: Exception) -> bool:
    """Check if an exception indicates a broken connection rather than a query error."""
    from clickhouse_connect.driver.exceptions import OperationalError
    if isinstance(err, (OSError, ConnectionError, OperationalError)):
        return True
    err_str = str(err).lower()
    return any(s in err_str for s in ("connection", "timed out", "reset by peer", "eof"))


def execute_query(query: str, query_id: str, client_config: dict):
    """Execute a query in a worker thread.

    Args:
        query: SQL to execute.
        query_id: Unique identifier for server-side tracking / cancellation.
        client_config: Pre-resolved config dict (resolved on the request thread).
    """
    cache_key = _config_to_cache_key(client_config)
    with _active_queries_lock:
        _active_queries[query_id] = (cache_key, query)

    try:
        client = create_clickhouse_client(config=client_config)
        _validate_query_for_destructive_ops(query)

        query_settings = build_query_settings(client)
        query_settings["query_id"] = query_id
        res = client.query(query, settings=query_settings)
        logger.info(f"Query {query_id} returned {len(res.result_rows)} rows")
        return {"columns": res.column_names, "rows": res.result_rows}
    except ToolError:
        raise
    except Exception as err:
        # Evict the cached client on connection errors so the next call
        # creates a fresh one. We do NOT retry here because the query may
        # involve writes and retrying could duplicate side effects.
        if _is_connection_error(err):
            _evict_cached_client(client_config)
        logger.error(f"Error executing query {query_id}: {err}")
        raise ToolError(f"Query execution failed: {str(err)}")
    finally:
        with _active_queries_lock:
            _active_queries.pop(query_id, None)


def _cancel_query(query_id: str):
    """Issue KILL QUERY on the ClickHouse server for a timed-out query.

    Uses the same cached client (same server/credentials) that originated
    the query. Failures are logged but never raised — cancellation errors
    must not mask the original timeout.
    """
    with _active_queries_lock:
        entry = _active_queries.pop(query_id, None)

    if entry is None:
        logger.debug("Query %s already completed, nothing to cancel", query_id)
        return

    cache_key, _query_text = entry
    try:
        with _client_cache_lock:
            cached = _client_cache.get(cache_key)
        if cached is None:
            logger.warning(
                "No cached client for query %s cancel — server-side query may still run",
                query_id,
            )
            return

        client, _ = cached
        logger.info("Cancelling query %s via KILL QUERY", query_id)
        client.command(f"KILL QUERY WHERE query_id = '{query_id}'")
        logger.info("Successfully cancelled query %s", query_id)
    except Exception as e:
        logger.warning("Failed to cancel query %s: %s", query_id, e)


def run_query(query: str):
    """Execute a SQL query against ClickHouse.

    Queries run in read-only mode by default. Set CLICKHOUSE_ALLOW_WRITE_ACCESS=true
    to allow DDL and DML statements when your ClickHouse server permits them.
    """
    logger.info(f"Executing query: {query}")

    # Resolve config on the request thread where FastMCP Context is available
    client_config = _resolve_client_config()
    query_id = str(uuid.uuid4())

    try:
        # Log pool utilization for observability using the pending work queue
        pending = QUERY_EXECUTOR._work_queue.qsize()
        with _active_queries_lock:
            in_flight = len(_active_queries)
        if in_flight + pending >= _max_workers:
            logger.warning(
                "Thread pool saturated: %d in-flight + %d queued vs %d workers",
                in_flight, pending, _max_workers,
            )

        future = QUERY_EXECUTOR.submit(execute_query, query, query_id, client_config)
        try:
            timeout_secs = get_mcp_config().query_timeout
            result = future.result(timeout=timeout_secs)
            # Check if we received an error structure from execute_query
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"Query failed: {result['error']}")
                # MCP requires structured responses; string error messages can cause
                # serialization issues leading to BrokenResourceError
                return {
                    "status": "error",
                    "message": f"Query failed: {result['error']}",
                }
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(
                "Query %s timed out after %s seconds: %s", query_id, timeout_secs, query
            )
            _cancel_query(query_id)
            raise ToolError(f"Query timed out after {timeout_secs} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error("Unexpected error in run_query: %s", str(e))
        raise RuntimeError(f"Unexpected error during query execution: {str(e)}")


def _config_to_cache_key(config: dict) -> tuple:
    """Convert a client config dict into a hashable cache key.

    Handles nested dicts (e.g. 'settings') by recursively sorting items.
    """
    items = []
    for k, v in sorted(config.items()):
        if isinstance(v, dict):
            v = _config_to_cache_key(v)
        items.append((k, v))
    return tuple(items)


def _resolve_client_config() -> dict:
    """Build the merged client config on the request thread.

    Must be called from the request thread where FastMCP Context is available.
    Merges base config with any per-session overrides, then aligns
    send_receive_timeout with the MCP query timeout.
    """
    client_config = get_config().get_client_config()
    srt_explicitly_set = "CLICKHOUSE_SEND_RECEIVE_TIMEOUT" in os.environ

    try:
        ctx = get_context()
        session_config_overrides = ctx.get_state(CLIENT_CONFIG_OVERRIDES_KEY)
        if session_config_overrides and not isinstance(session_config_overrides, dict):
            logger.warning(
                f"{CLIENT_CONFIG_OVERRIDES_KEY} must be a dict, got {type(session_config_overrides).__name__}. Ignoring."
            )
        elif session_config_overrides:
            logger.debug(
                f"Applying session-specific ClickHouse client config overrides: {list(session_config_overrides.keys())}"
            )
            if "send_receive_timeout" in session_config_overrides:
                srt_explicitly_set = True
            client_config.update(session_config_overrides)
    except RuntimeError:
        # Outside a request context — proceed with base config
        pass

    # Align send_receive_timeout with MCP query timeout so worker threads
    # unblock shortly after the MCP-level timeout fires, preventing zombie threads.
    # Only auto-cap when neither env var nor session override explicitly set it.
    if not srt_explicitly_set:
        query_timeout = get_mcp_config().query_timeout
        effective_srt = client_config.get("send_receive_timeout", 300)
        if effective_srt > query_timeout + 5:
            client_config["send_receive_timeout"] = query_timeout + 5

    return client_config


def _evict_cached_client(config: dict) -> None:
    """Evict a cached client for the given config, closing it.

    Call this when a query or command fails with a connection error so the
    next call creates a fresh client instead of reusing the broken one.
    """
    cache_key = _config_to_cache_key(config)
    with _client_cache_lock:
        entry = _client_cache.pop(cache_key, None)
    if entry is not None:
        client, _ = entry
        logger.info("Evicted stale cached client for %s", config.get("host", "?"))
        try:
            client.close()
        except Exception:
            pass


def create_clickhouse_client(config: Optional[dict] = None):
    """Get or create a cached ClickHouse client for the given config.

    Args:
        config: Pre-resolved client config dict.  When None the config is
                resolved from env + session overrides (requires request context).
                Pass an explicit config when calling from a worker thread.
    """
    if config is None:
        config = _resolve_client_config()

    cache_key = _config_to_cache_key(config)

    # Check cache — extract candidate without holding the lock during ping
    candidate = None
    with _client_cache_lock:
        if cache_key in _client_cache:
            client, last_used = _client_cache[cache_key]
            if time.time() - last_used > _CLIENT_IDLE_PING_THRESHOLD:
                candidate = client
            else:
                _client_cache[cache_key] = (client, time.time())
                logger.debug("Reusing cached client")
                return client

    # Ping outside the lock so we don't serialize unrelated configs
    if candidate is not None:
        try:
            alive = candidate.ping()
        except Exception:
            alive = False

        if alive:
            with _client_cache_lock:
                # Re-check: another thread may have evicted while we pinged
                if cache_key in _client_cache:
                    _client_cache[cache_key] = (candidate, time.time())
                    logger.debug("Reusing cached client (ping OK after idle)")
                    return candidate
                # Was evicted by another thread; fall through to create new
        else:
            logger.warning("Cached client failed ping, creating new client")
            with _client_cache_lock:
                _client_cache.pop(cache_key, None)
            try:
                candidate.close()
            except Exception:
                pass

    # Create new client outside the lock (client creation is slow)
    config_fields = [
        f"secure={config['secure']}",
        f"verify={config['verify']}",
        f"connect_timeout={config['connect_timeout']}s",
        f"send_receive_timeout={config['send_receive_timeout']}s",
    ]
    if "server_host_name" in config:
        config_fields.append(f"server_host_name={config['server_host_name']}")
    logger.info(
        f"Creating ClickHouse client connection to {config['host']}:{config['port']} "
        f"as {config['username']} "
        f"({', '.join(config_fields)})"
    )

    try:
        # Disable autogenerate_session_id so the client is safe for concurrent
        # use from the thread pool. clickhouse_connect rejects concurrent queries
        # on the same session_id, but with this disabled each query runs
        # without session affinity.
        client = clickhouse_connect.get_client(
            **config, autogenerate_session_id=False
        )
        version = client.server_version
        logger.info(f"Successfully connected to ClickHouse server version {version}")
    except Exception as e:
        logger.error(f"Failed to connect to ClickHouse: {str(e)}")
        raise

    with _client_cache_lock:
        # Another thread may have raced and cached a client for this key
        if cache_key in _client_cache:
            try:
                client.close()
            except Exception:
                pass
            client, _ = _client_cache[cache_key]
            _client_cache[cache_key] = (client, time.time())
            return client
        _client_cache[cache_key] = (client, time.time())

    return client


def _clear_client_cache():
    """Clear the client cache, closing all cached clients.

    Used during shutdown and for testing.
    """
    with _client_cache_lock:
        for _, (client, _) in list(_client_cache.items()):
            try:
                client.close()
            except Exception:
                pass
        _client_cache.clear()


atexit.register(_clear_client_cache)


def build_query_settings(client) -> dict[str, str]:
    """Build query settings dict for ClickHouse queries.

    Always returns a dict (possibly empty) to ensure consistent behavior.
    """
    readonly_setting = get_readonly_setting(client)
    if readonly_setting is not None:
        return {"readonly": readonly_setting}
    return {}


def get_readonly_setting(client) -> Optional[str]:
    """Determine the readonly setting value for queries.

    This implements the following logic:
    1. If CLICKHOUSE_ALLOW_WRITE_ACCESS=true (writes enabled):
       - Allow writes if server permits (server readonly=None or "0")
       - Fall back to server's readonly setting if server enforces it
       - Log a warning when falling back

    2. If CLICKHOUSE_ALLOW_WRITE_ACCESS=false (default, read-only mode):
       - Enforce readonly=1 if server allows writes
       - Respect server's readonly setting if server enforces stricter mode

    Returns:
        "0" = writes allowed
        "1" = read-only mode (allows SET of non-privileged settings)
        "2" = strict read-only (server enforced; disallows SET)
        None = use server default (shouldn't happen in practice)
    """
    config = get_config()
    server_settings = getattr(client, "server_settings", {}) or {}
    server_readonly = _normalize_readonly_value(server_settings.get("readonly"))

    # Case 1: User wants write access (CLICKHOUSE_ALLOW_WRITE_ACCESS=true)
    if config.allow_write_access:
        if server_readonly in (None, "0"):
            logger.info("Write mode enabled (CLICKHOUSE_ALLOW_WRITE_ACCESS=true)")
            return "0"

        # If server forbids writes, respect server configuration
        logger.warning(
            "CLICKHOUSE_ALLOW_WRITE_ACCESS=true but server enforces readonly=%s; "
            "write operations will fail",
            server_readonly,
        )
        return server_readonly

    # Case 2: User wants read-only mode (CLICKHOUSE_ALLOW_WRITE_ACCESS=false, default)
    if server_readonly in (None, "0"):
        return "1"  # Enforce read-only since server allows writes

    return server_readonly  # Server already enforces readonly, respect it


def _normalize_readonly_value(value: Any) -> Optional[str]:
    """Normalize ClickHouse readonly setting to a simple string.

    The clickhouse_connect library represents settings as objects with a .value attribute.
    This function extracts the actual value for our logic.

    Args:
        value: The readonly setting value from ClickHouse server. Can be:
            - None (server has no readonly restriction)
            - A clickhouse_connect setting object with a .value attribute
            - An int (0, 1, 2)
            - A str ("0", "1", "2")

    Returns:
        Optional[str]: Normalized readonly value as string ("0", "1", "2") or None
    """
    if value is None:
        return None

    # Extract value from clickhouse_connect setting object
    if hasattr(value, "value"):
        value = value.value

    return str(value)


def create_chdb_client():
    """Create a chDB client connection."""
    if not get_chdb_config().enabled:
        raise ValueError("chDB is not enabled. Set CHDB_ENABLED=true to enable it.")
    if _chdb_client is None:
        raise RuntimeError(_chdb_error_message or "chDB client is not available.")
    return _chdb_client


def execute_chdb_query(query: str):
    """Execute a query using chDB client."""
    client = create_chdb_client()
    try:
        res = client.query(query, "JSON")
        if res.has_error():
            error_msg = res.error_message()
            logger.error(f"Error executing chDB query: {error_msg}")
            return {"error": error_msg}

        result_data = res.data()
        if not result_data:
            return []

        result_json = json.loads(result_data)

        return result_json.get("data", [])

    except Exception as err:
        logger.error(f"Error executing chDB query: {err}")
        return {"error": str(err)}


def run_chdb_select_query(query: str):
    """Run SQL in chDB, an in-process ClickHouse engine"""
    logger.info(f"Executing chDB SELECT query: {query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_chdb_query, query)
        try:
            timeout_secs = get_mcp_config().query_timeout
            result = future.result(timeout=timeout_secs)
            # Check if we received an error structure from execute_chdb_query
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"chDB query failed: {result['error']}")
                return {
                    "status": "error",
                    "message": f"chDB query failed: {result['error']}",
                }
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(f"chDB query timed out after {timeout_secs} seconds: {query}")
            future.cancel()
            return {
                "status": "error",
                "message": f"chDB query timed out after {timeout_secs} seconds",
            }
    except Exception as e:
        logger.error(f"Unexpected error in run_chdb_select_query: {e}")
        return {"status": "error", "message": f"Unexpected error: {e}"}


def chdb_initial_prompt() -> str:
    """This prompt helps users understand how to interact and perform common operations in chDB"""
    return CHDB_PROMPT


def _init_chdb_client():
    """Initialize the global chDB client instance."""
    global _chdb_error_message
    try:
        if not get_chdb_config().enabled:
            logger.info("chDB is disabled, skipping client initialization")
            _chdb_error_message = None
            return None

        client_config = get_chdb_config().get_client_config()
        data_path = client_config["data_path"]
        logger.info(f"Creating chDB client with data_path={data_path}")
        import chdb.session as chs

        client = chs.Session(path=data_path)
        _chdb_error_message = None
        logger.info(f"Successfully connected to chDB with data_path={data_path}")
        return client
    except ModuleNotFoundError as e:
        if e.name in {"chdb", "chdb.session"}:
            _chdb_error_message = (
                "chDB support requires the optional dependency. "
                "Install mcp-clickhouse[chdb] to enable chDB features."
            )
            logger.warning(_chdb_error_message)
            return None
        _chdb_error_message = f"Failed to initialize chDB client: {e}"
        logger.error(_chdb_error_message)
        return None
    except ImportError as e:
        _chdb_error_message = f"Failed to initialize chDB client: {e}"
        logger.error(_chdb_error_message)
        return None
    except Exception as e:
        _chdb_error_message = f"Failed to initialize chDB client: {e}"
        logger.error(_chdb_error_message)
        return None


def _register_chdb_tools():
    """Register chDB tools when the feature is enabled and available.

    Note: This function is not idempotent. Calling it multiple times will
    register duplicate tools. It is intended to be called once at module load.
    """
    global _chdb_client
    if not get_chdb_config().enabled:
        return

    _chdb_client = _init_chdb_client()
    if _chdb_client is None:
        logger.warning("chDB is enabled but unavailable; skipping chDB tool registration")
        return

    atexit.register(_chdb_client.close)
    mcp.add_tool(Tool.from_function(run_chdb_select_query))
    chdb_prompt = Prompt.from_function(
        chdb_initial_prompt,
        name="chdb_initial_prompt",
        description="This prompt helps users understand how to interact and perform common operations in chDB",
    )
    mcp.add_prompt(chdb_prompt)
    logger.info("chDB tools and prompts registered")


# Register tools based on configuration
if os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true":
    mcp.add_tool(Tool.from_function(list_databases))
    mcp.add_tool(Tool.from_function(list_tables))
    mcp.add_tool(
        Tool.from_function(
            run_query,
            description=(
                "Execute SQL queries in ClickHouse. Queries run in read-only mode by default. "
                "Set CLICKHOUSE_ALLOW_WRITE_ACCESS=true to allow DDL and DML operations. "
                "Set CLICKHOUSE_ALLOW_DROP=true to additionally allow destructive operations (DROP, TRUNCATE)."
            ),
        )
    )
    logger.info("ClickHouse tools registered")


_register_chdb_tools()
