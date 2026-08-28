import asyncio
import atexit
import concurrent.futures
import inspect
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from ipaddress import IPv4Network, IPv6Network
from typing import Any, Dict, List, Optional

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
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mcp_clickhouse.chdb_prompt import CHDB_PROMPT
from mcp_clickhouse.http_security import transport_security_middleware
from mcp_clickhouse.mcp_env import TransportType, get_chdb_config, get_config, get_mcp_config
from mcp_clickhouse.skills_advisor import CLICKHOUSE_SERVER_INSTRUCTIONS


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
_CLIENT_CONFIG_OVERRIDES_UNSET = object()
_NESTED_CLIENT_CONFIG_KEYS = ("settings", "generic_args")
_REJECTED_ROLE_OVERRIDE_KEYS = ("role", "ch_role")

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(MCP_SERVER_NAME)

QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)
atexit.register(lambda: QUERY_EXECUTOR.shutdown(wait=True))

load_dotenv()

_HTTP_TRANSPORTS = (TransportType.HTTP.value, "streamable-http", TransportType.SSE.value)
_BUILTIN_HTTP_RAW_CLIENT = ContextVar("builtin_http_raw_client", default=False)


def _resolve_auth(mcp_config, transport: Optional[str] = None) -> Dict[str, Any]:
    """Resolve FastMCP auth kwargs for the requested transport.

    An empty return dict omits the `auth` kwarg so FastMCP auto-detects its
    provider from FASTMCP_SERVER_AUTH / FASTMCP_SERVER_AUTH_* env vars.
    Returning {"auth": None} instead explicitly disables auth.
    """
    transport = transport or mcp_config.server_transport
    if transport not in _HTTP_TRANSPORTS:
        return {}

    configured = {
        "CLICKHOUSE_MCP_AUTH_DISABLED": mcp_config.auth_disabled,
        "CLICKHOUSE_MCP_AUTH_TOKEN": bool(mcp_config.auth_token),
        "FASTMCP_SERVER_AUTH": bool(os.getenv("FASTMCP_SERVER_AUTH")),
    }
    active = [name for name, is_set in configured.items() if is_set]

    if len(active) > 1:
        raise ValueError(
            "Multiple authentication modes configured for HTTP/SSE transport: "
            f"{', '.join(active)}. These are mutually exclusive; unset all but one."
        )

    if not active:
        raise ValueError(
            "Authentication is required for HTTP/SSE transports. Configure exactly one of:\n"
            "  - CLICKHOUSE_MCP_AUTH_TOKEN=<token>   (static bearer token)\n"
            "  - FASTMCP_SERVER_AUTH=<class-path>    (FastMCP auth provider, full class path;\n"
            "       e.g. fastmcp.server.auth.providers.azure.AzureProvider)\n"
            "  - CLICKHOUSE_MCP_AUTH_DISABLED=true   (disables auth; development only)"
        )

    if mcp_config.auth_disabled:
        logger.warning("WARNING: MCP SERVER AUTHENTICATION IS DISABLED")
        logger.warning("Only use this for local development/testing.")
        logger.warning("DO NOT expose to networks.")
        return {"auth": None}

    if mcp_config.auth_token:
        verifier = StaticTokenVerifier(
            tokens={mcp_config.auth_token: {"client_id": "mcp-client", "scopes": []}},
            required_scopes=[],
        )
        logger.info("Authentication enabled for HTTP/SSE transport (static bearer token)")
        return {"auth": verifier}

    logger.info(
        "Authentication delegated to FastMCP provider: %s", os.getenv("FASTMCP_SERVER_AUTH")
    )
    # Return empty kwargs so FastMCP auto-loads from FASTMCP_SERVER_AUTH_* env vars.
    return {}


def _proxy_header_trusted_hosts(
    trusted_proxies: List[IPv4Network | IPv6Network],
) -> List[str]:
    """Uvicorn trusted_hosts entries with IPv4-mapped IPv6 forms added.

    Uvicorn compares the raw peer without unmapping, so on a dual-stack bind an
    IPv4 proxy seen as ::ffff:a.b.c.d only matches the mapped form.
    """
    trusted_hosts = []
    for network in trusted_proxies:
        trusted_hosts.append(str(network))
        if isinstance(network, IPv4Network):
            trusted_hosts.append(f"::ffff:{network.network_address}/{network.prefixlen + 96}")
    return trusted_hosts


class ClickHouseFastMCP(FastMCP):
    """FastMCP server that secures every constructed HTTP transport app."""

    def http_app(
        self,
        *args: Any,
        raw_client_address_preserved: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Create an authenticated HTTP app with Host and Origin validation."""
        upstream_http_app = super().http_app
        bound_args = inspect.signature(upstream_http_app).bind_partial(*args, **kwargs)
        transport = bound_args.arguments.get("transport", TransportType.HTTP.value)
        mcp_config = get_mcp_config()
        trusted_proxies = mcp_config.trusted_proxies
        if _BUILTIN_HTTP_RAW_CLIENT.get():
            # Consume the runner's single app construction so later calls in
            # the same context do not inherit the assertion.
            _BUILTIN_HTTP_RAW_CLIENT.set(False)
            raw_client_address_preserved = True
        if trusted_proxies and not raw_client_address_preserved:
            raise ValueError(
                "CLICKHOUSE_MCP_TRUSTED_PROXIES requires a raw ASGI client address. "
                "Disable proxy-header processing in the outer ASGI server and pass "
                "raw_client_address_preserved=True."
            )

        auth_kwargs = _resolve_auth(mcp_config, transport=transport)
        original_auth = self.auth
        if "auth" in auth_kwargs:
            app_auth = auth_kwargs["auth"]
        elif original_auth is None:
            raise ValueError("FASTMCP_SERVER_AUTH did not create an authentication provider")
        else:
            app_auth = original_auth

        self.auth = app_auth
        try:
            app = upstream_http_app(*args, **kwargs)
        finally:
            self.auth = original_auth
        if getattr(app.state, "path", None) == "/health":
            raise ValueError(
                "MCP transport path cannot be /health because that path is reserved "
                "for the public health endpoint"
            )
        if trusted_proxies:
            app.add_middleware(
                ProxyHeadersMiddleware,
                trusted_hosts=_proxy_header_trusted_hosts(trusted_proxies),
            )
        for configured_middleware in transport_security_middleware(mcp_config):
            app.add_middleware(configured_middleware.cls, **configured_middleware.kwargs)
        return app

    def sse_app(
        self,
        path: Optional[str] = None,
        message_path: Optional[str] = None,
        middleware: Optional[list] = None,
        *,
        raw_client_address_preserved: bool = False,
    ) -> Any:
        """Create a secured SSE app.

        FastMCP 2.12 and 2.13 build this app without going through http_app,
        skipping auth and transport validation. Deprecated upstream; prefer
        http_app(transport="sse"). Intended for startup-time construction: the
        temporary message_path settings mutation is not concurrency-safe.
        """
        settings = self._deprecated_settings
        original_message_path = settings.message_path
        if message_path is not None:
            settings.message_path = message_path
        try:
            return self.http_app(
                path=path,
                middleware=middleware,
                transport=TransportType.SSE.value,
                raw_client_address_preserved=raw_client_address_preserved,
            )
        finally:
            settings.message_path = original_message_path

    def streamable_http_app(
        self,
        path: Optional[str] = None,
        middleware: Optional[list] = None,
        *,
        raw_client_address_preserved: bool = False,
    ) -> Any:
        """Create a secured streamable HTTP app.

        Deprecated upstream; prefer http_app().
        """
        return self.http_app(
            path=path,
            middleware=middleware,
            transport=TransportType.HTTP.value,
            raw_client_address_preserved=raw_client_address_preserved,
        )

    async def run_http_async(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Run HTTP with Host validation before trusted proxy header processing."""
        upstream_run_http = super().run_http_async
        trusted_proxies = get_mcp_config().trusted_proxies
        if not trusted_proxies:
            await upstream_run_http(*args, **kwargs)
            return

        upstream_signature = inspect.signature(upstream_run_http)
        if "uvicorn_config" not in upstream_signature.parameters:
            raise RuntimeError(
                "CLICKHOUSE_MCP_TRUSTED_PROXIES requires a FastMCP version whose "
                "HTTP runner supports uvicorn_config"
            )
        bound_args = upstream_signature.bind_partial(*args, **kwargs)
        inner_uvicorn_config = dict(bound_args.arguments.get("uvicorn_config") or {})
        if inner_uvicorn_config.get("proxy_headers"):
            raise ValueError(
                "uvicorn_config['proxy_headers'] must be false when "
                "CLICKHOUSE_MCP_TRUSTED_PROXIES is configured"
            )
        inner_uvicorn_config["proxy_headers"] = False
        bound_args.arguments["uvicorn_config"] = inner_uvicorn_config
        token = _BUILTIN_HTTP_RAW_CLIENT.set(True)
        try:
            await upstream_run_http(*bound_args.args, **bound_args.kwargs)
        finally:
            _BUILTIN_HTTP_RAW_CLIENT.reset(token)


mcp = ClickHouseFastMCP(
    name=MCP_SERVER_NAME,
    instructions=CLICKHOUSE_SERVER_INSTRUCTIONS,
)
_chdb_client = None
_chdb_error_message: Optional[str] = None


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    """Liveness probe. Intentionally unauthenticated and minimal.

    Debug via server logs.
    """
    try:
        # Check if ClickHouse is enabled by trying to create config
        # If ClickHouse is disabled, this will succeed but connection will fail
        clickhouse_enabled = os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true"

        if not clickhouse_enabled:
            # If ClickHouse is disabled, check chDB status
            chdb_config = get_chdb_config()
            if chdb_config.enabled and _chdb_client is not None:
                return PlainTextResponse("OK")
            elif chdb_config.enabled and _chdb_error_message:
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

        # Try to create a client connection to verify ClickHouse connectivity
        create_clickhouse_client()
        return PlainTextResponse("OK")
    except Exception:
        # Log the underlying error server-side, but don't leak details over the wire.
        logger.exception("Health check failed: ClickHouse connection error")
        return PlainTextResponse(
            "ERROR. ClickHouse connection failed. Check server logs for details.",
            status_code=503,
        )


def result_to_table(query_columns, result) -> List[Table]:
    return [Table(**dict(zip(query_columns, row))) for row in result]


def result_to_column(query_columns, result) -> List[Column]:
    return [Column(**dict(zip(query_columns, row))) for row in result]


def _serialize_tool_result(obj: Any) -> str:
    return json.dumps(obj, default=str)


def list_databases() -> str:
    """List available ClickHouse databases"""
    logger.info("Listing all databases")
    client = create_clickhouse_client()
    result = client.command("SHOW DATABASES")

    # Convert newline-separated string to list and trim whitespace
    if isinstance(result, str):
        databases = [db.strip() for db in result.strip().split("\n")]
    else:
        databases = [result]

    logger.info(f"Found {len(databases)} databases")
    return _serialize_tool_result(databases)


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
) -> str:
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
        A JSON-encoded string of an object containing:
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
    client = create_clickhouse_client()

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
            return _serialize_tool_result({
                "tables": [asdict(table) for table in tables],
                "next_page_token": next_page_token,
                "total_tables": len(table_names),
            })

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

    return _serialize_tool_result({
        "tables": [asdict(table) for table in tables],
        "next_page_token": next_page_token,
        "total_tables": len(table_names),
    })


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


def execute_query(
    query: str, client_config_overrides: Optional[dict[str, Any]] = None
) -> str:
    client = create_clickhouse_client(client_config_overrides)
    try:
        _validate_query_for_destructive_ops(query)

        query_settings = build_query_settings(client)
        res = client.query(query, settings=query_settings)
        logger.info(f"Query returned {len(res.result_rows)} rows")
        return _serialize_tool_result({"columns": res.column_names, "rows": res.result_rows})
    except ToolError:
        raise
    except Exception as err:
        logger.error(f"Error executing query: {err}")
        raise ToolError(f"Query execution failed: {str(err)}")


def run_query(query: str) -> str:
    """Execute a SQL query against ClickHouse.

    Queries run in read-only mode by default. Set CLICKHOUSE_ALLOW_WRITE_ACCESS=true
    to allow DDL and DML statements when your ClickHouse server permits them.
    """
    logger.info(f"Executing query: {query}")
    try:
        client_config_overrides = _get_client_config_overrides()
        future = QUERY_EXECUTOR.submit(execute_query, query, client_config_overrides)
        timeout_secs = get_mcp_config().query_timeout
        try:
            return future.result(timeout=timeout_secs)
        except concurrent.futures.TimeoutError:
            logger.warning(f"Query timed out after {timeout_secs} seconds: {query}")
            future.cancel()
            raise ToolError(f"Query timed out after {timeout_secs} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error("Unexpected error in run_query: %s", str(e))
        raise RuntimeError(f"Unexpected error during query execution: {str(e)}")


async def run_query_async(query: str) -> str:
    """Async MCP-facing wrapper for ClickHouse queries."""
    logger.info(f"Executing query: {query}")
    try:
        client_config_overrides = _get_client_config_overrides()
        future = QUERY_EXECUTOR.submit(execute_query, query, client_config_overrides)
        timeout_secs = get_mcp_config().query_timeout
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=timeout_secs
            )
        except asyncio.TimeoutError:
            logger.warning(f"Query timed out after {timeout_secs} seconds: {query}")
            future.cancel()
            raise ToolError(f"Query timed out after {timeout_secs} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error("Unexpected error in run_query_async: %s", str(e))
        raise RuntimeError(f"Unexpected error during query execution: {str(e)}")


# ClickHouse native TCP protocol ports (clickhouse-client). This MCP server uses the
# HTTP interface only (default 8123 / 8443). Connecting to native ports fails with
# messages like "Port 9000 is for clickhouse-client program".
_NATIVE_PROTOCOL_PORTS = frozenset({9000, 9440})


def _connection_error_hints(error: Exception, client_config: dict) -> List[str]:
    """Return actionable hints for common ClickHouse connection misconfigurations.

    Helps users who confuse MCP transport settings with database settings, or who
    point CLICKHOUSE_PORT at the native TCP protocol instead of the HTTP interface.
    """
    hints: List[str] = []
    err = str(error).lower()
    port = client_config.get("port")
    secure = bool(client_config.get("secure"))
    host = client_config.get("host", "<unknown>")

    native_response_port = next(
        (
            native_port
            for native_port in _NATIVE_PROTOCOL_PORTS
            if f"port {native_port} is for clickhouse-client" in err
        ),
        None,
    )
    if port in _NATIVE_PROTOCOL_PORTS:
        hints.append(
            f"CLICKHOUSE_PORT={port} looks like ClickHouse's native TCP protocol port "
            "(used by clickhouse-client). This server uses the HTTP interface — set "
            "CLICKHOUSE_PORT to 8123 (HTTP) or 8443 (HTTPS), or your deployment's HTTP "
            "mapping. Do not use native ports 9000/9440."
        )
    elif native_response_port is not None:
        hints.append(
            f"The ClickHouse response indicates that this request reached native TCP port "
            f"{native_response_port}, even though the client was configured for {host}:{port}. "
            "Check DNS, service, proxy, load-balancer, and port mappings to ensure traffic is "
            "routed to ClickHouse's HTTP interface (8123/8443 by default, or your deployment's "
            "HTTP mapping)."
        )

    tls_tokens = (
        "ssl",
        "tls",
        "certificate",
        "handshake",
        "wrong version number",
        "certificate verify failed",
        "unexpected_eof",
        "eof occurred in violation of protocol",
    )
    if any(token in err for token in tls_tokens):
        scheme = "HTTPS" if secure else "HTTP"
        hints.append(
            f"TLS/SSL error while connecting with CLICKHOUSE_SECURE="
            f"{str(secure).lower()} ({scheme} to {host}:{port}). "
            "CLICKHOUSE_SECURE enables HTTPS for the ClickHouse database connection "
            "only — it is not MCP or ingress TLS. Use true for HTTPS database "
            "endpoints (ClickHouse Cloud / port 8443) and false only for plain HTTP "
            "(typical local Docker on 8123)."
        )

    # General connectivity and scheme/port failures can surface as opaque HTTP errors.
    connection_failure_tokens = (
        "http status",
        "bad status line",
        "connection refused",
        "connection reset",
        "remote end closed connection",
    )
    if any(token in err for token in connection_failure_tokens) and not hints:
        hints.append(
            f"Connection to {host}:{port} failed. Verify ClickHouse is running and reachable "
            "at this address and that network or proxy routing permits access. Then confirm "
            f"CLICKHOUSE_SECURE={str(secure).lower()} matches whether ClickHouse expects HTTPS, "
            "and that CLICKHOUSE_PORT is an HTTP interface port (8123/8443), not a native TCP "
            "port (9000/9440). These settings configure the database client, not the MCP "
            "server transport."
        )

    return hints


def _format_connection_failure(error: Exception, client_config: dict) -> str:
    """Build a connection failure message with optional configuration hints."""
    message = f"Failed to connect to ClickHouse: {error}"
    hints = _connection_error_hints(error, client_config)
    if hints:
        message += "\n" + "\n".join(f"Hint: {hint}" for hint in hints)
    return message


# Privileges the drop gate pretends to block but cannot enforce server-side.
# ALTER includes ALTER DELETE and ALTER DROP PARTITION in the privilege
# hierarchy. ALTER ADD is exempt because the README recipe grants it.
_GRANTS_ADVISORY_PRIVILEGES = re.compile(
    r"\b(ALL|DROP|TRUNCATE|DELETE|UPDATE|ALTER\b(?!\s+ADD\b))\b", re.IGNORECASE
)

_grants_advisory_done = False


def _warn_if_overprivileged(client) -> None:
    """Warn once when the drop gate is active but the ClickHouse user holds
    privileges it cannot enforce against. Fail-open, never raises.
    """
    global _grants_advisory_done
    if _grants_advisory_done:
        return
    # Check-then-set race across executor threads is harmless, worst case is a
    # duplicate warning.
    _grants_advisory_done = True

    try:
        result = client.query("SHOW GRANTS")
        matched: set[str] = set()
        role_grants = []
        for row in result.result_rows:
            grant = str(row[0])
            if grant.upper().startswith("GRANT") and not re.search(r"\bON\b", grant, re.IGNORECASE):
                # `GRANT <role> TO ...`; role privileges are not expanded here.
                role_grants.append(grant)
                continue
            matched.update(m.group(1).upper() for m in _GRANTS_ADVISORY_PRIVILEGES.finditer(grant))
        if matched:
            logger.warning(
                "CLICKHOUSE_ALLOW_DROP=false, but the ClickHouse user holds %s privileges. "
                "The destructive-operation gate runs in the MCP server and is not enforced "
                "server-side. See the README least-privilege recipe to restrict grants.",
                ", ".join(sorted(matched)),
            )
        for grant in role_grants:
            logger.info(
                "Grants advisory cannot inspect privileges granted via roles: %s", grant
            )
    except Exception as e:
        logger.debug("Grants advisory skipped: %s", e)


def _snapshot_client_config_overrides(overrides: Any) -> Optional[dict[str, Any]]:
    """Validate and copy request-scoped ClickHouse client overrides."""
    if overrides is None:
        return None
    if not isinstance(overrides, dict):
        raise ToolError(f"{CLIENT_CONFIG_OVERRIDES_KEY} must be a dict")

    snapshot = dict(overrides)
    for key in _REJECTED_ROLE_OVERRIDE_KEYS:
        if key in snapshot:
            raise ToolError(
                f"{CLIENT_CONFIG_OVERRIDES_KEY}.{key} is not supported; "
                f"use {CLIENT_CONFIG_OVERRIDES_KEY}.settings.role"
            )
    for key in _NESTED_CLIENT_CONFIG_KEYS:
        if key not in snapshot:
            continue
        value = snapshot[key]
        if not isinstance(value, Mapping):
            raise ToolError(f"{CLIENT_CONFIG_OVERRIDES_KEY}.{key} must be a mapping")
        if key == "generic_args":
            for role_key in _REJECTED_ROLE_OVERRIDE_KEYS:
                if role_key in value:
                    raise ToolError(
                        f"{CLIENT_CONFIG_OVERRIDES_KEY}.generic_args.{role_key} "
                        "is not supported; "
                        f"use {CLIENT_CONFIG_OVERRIDES_KEY}.settings.role"
                    )
        snapshot[key] = dict(value)
    return snapshot


def _get_client_config_overrides() -> Optional[dict[str, Any]]:
    """Capture ClickHouse client overrides from the active FastMCP request."""
    try:
        ctx = get_context()
    except RuntimeError:
        return None
    return _snapshot_client_config_overrides(ctx.get_state(CLIENT_CONFIG_OVERRIDES_KEY))


def _apply_client_config_overrides(
    client_config: dict[str, Any], overrides: Optional[dict[str, Any]]
) -> None:
    """Merge request-scoped overrides into the base client configuration."""
    if overrides is None:
        return

    logger.debug(
        "Applying request-specific ClickHouse client config override keys: %s",
        list(overrides.keys()),
    )
    remaining_overrides = dict(overrides)
    for key in _NESTED_CLIENT_CONFIG_KEYS:
        if key not in remaining_overrides:
            continue
        base_value = client_config.get(key, {})
        if base_value is None:
            base_value = {}
        if not isinstance(base_value, Mapping):
            raise ToolError(f"Base ClickHouse client config {key} must be a mapping")
        client_config[key] = {**base_value, **remaining_overrides.pop(key)}
    client_config.update(remaining_overrides)


def create_clickhouse_client(client_config_overrides=_CLIENT_CONFIG_OVERRIDES_UNSET):
    if client_config_overrides is _CLIENT_CONFIG_OVERRIDES_UNSET:
        overrides = _get_client_config_overrides()
    else:
        overrides = _snapshot_client_config_overrides(client_config_overrides)
    overrides_applied = bool(overrides)

    client_config = get_config().get_client_config()
    _apply_client_config_overrides(client_config, overrides)

    port = client_config.get("port")
    if port in _NATIVE_PROTOCOL_PORTS:
        logger.warning(
            "CLICKHOUSE_PORT=%s is a native TCP protocol port (clickhouse-client). "
            "mcp-clickhouse uses the HTTP interface; prefer 8123 (HTTP) or 8443 (HTTPS).",
            port,
        )

    config_fields = [
        f"secure={client_config['secure']}",
        f"verify={client_config['verify']}",
        f"connect_timeout={client_config['connect_timeout']}s",
        f"send_receive_timeout={client_config['send_receive_timeout']}s",
    ]
    if "server_host_name" in client_config:
        config_fields.append(f"server_host_name={client_config['server_host_name']}")
    log_msg = (
        f"Creating ClickHouse client connection to {client_config['host']}:{client_config['port']} "
        f"as {client_config['username']} "
        f"({', '.join(config_fields)})"
    )
    logger.info(log_msg)

    try:
        client = clickhouse_connect.get_client(**client_config)
        # Test the connection
        version = client.server_version
        logger.info(f"Successfully connected to ClickHouse server version {version}")
    except Exception as e:
        message = _format_connection_failure(e, client_config)
        logger.error(message)
        raise

    config = get_config()
    # Session overrides may connect as a different user. Skip the advisory
    # without consuming the one-shot so a later base-config client still runs it.
    if not overrides_applied and config.allow_write_access and not config.allow_drop:
        _warn_if_overprivileged(client)
    return client


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


def _process_chdb_result(result) -> str:
    if isinstance(result, dict) and "error" in result:
        logger.warning(f"chDB query failed: {result['error']}")
        return _serialize_tool_result({
            "status": "error",
            "message": f"chDB query failed: {result['error']}",
        })
    return _serialize_tool_result(result)


def run_chdb_select_query(query: str) -> str:
    """Run SQL in chDB, an in-process ClickHouse engine"""
    logger.info(f"Executing chDB SELECT query: {query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_chdb_query, query)
        timeout_secs = get_mcp_config().query_timeout
        try:
            result = future.result(timeout=timeout_secs)
            return _process_chdb_result(result)
        except concurrent.futures.TimeoutError:
            logger.warning(f"chDB query timed out after {timeout_secs} seconds: {query}")
            future.cancel()
            return _serialize_tool_result({
                "status": "error",
                "message": f"chDB query timed out after {timeout_secs} seconds",
            })
    except Exception as e:
        logger.error(f"Unexpected error in run_chdb_select_query: {e}")
        return _serialize_tool_result({"status": "error", "message": f"Unexpected error: {e}"})


async def run_chdb_select_query_async(query: str) -> str:
    """Async MCP-facing wrapper for chDB queries."""
    logger.info(f"Executing chDB SELECT query: {query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_chdb_query, query)
        timeout_secs = get_mcp_config().query_timeout
        try:
            result = await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=timeout_secs
            )
            return _process_chdb_result(result)
        except asyncio.TimeoutError:
            logger.warning(
                f"chDB query timed out after {timeout_secs} seconds: {query}"
            )
            future.cancel()
            return _serialize_tool_result({
                "status": "error",
                "message": f"chDB query timed out after {timeout_secs} seconds",
            })
    except Exception as e:
        logger.error(f"Unexpected error in run_chdb_select_query_async: {e}")
        return _serialize_tool_result({"status": "error", "message": f"Unexpected error: {e}"})


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
    mcp.add_tool(
        Tool.from_function(
            run_chdb_select_query_async,
            name="run_chdb_select_query",
            description="Run SQL in chDB, an in-process ClickHouse engine",
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
    mcp.add_tool(Tool.from_function(list_databases))
    mcp.add_tool(Tool.from_function(list_tables))
    mcp.add_tool(
        Tool.from_function(
            run_query_async,
            name="run_query",
            description=(
                "Execute SQL queries in ClickHouse. Queries run in read-only mode by default. "
                "Set CLICKHOUSE_ALLOW_WRITE_ACCESS=true to allow DDL and DML operations. "
                "Set CLICKHOUSE_ALLOW_DROP=true to additionally allow destructive operations "
                "(DROP, TRUNCATE, DELETE, UPDATE, REPLACE TABLE/PARTITION, CREATE OR REPLACE, "
                "CLEAR COLUMN/INDEX/PROJECTION, DETACH PERMANENTLY). That gate is a best-effort "
                "accident guard, not a security boundary."
            ),
        )
    )
    logger.info("ClickHouse tools registered")


_register_chdb_tools()
