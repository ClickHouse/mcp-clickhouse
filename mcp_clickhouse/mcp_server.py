import atexit
import concurrent.futures
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Dict, List, Optional, TypedDict

import chdb.session as chs
import clickhouse_connect
from cachetools import TTLCache
from clickhouse_connect.driver.binding import format_query_value
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.prompts import Prompt
from fastmcp.tools import Tool
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_clickhouse.chdb_prompt import CHDB_PROMPT
from mcp_clickhouse.mcp_env import get_chdb_config, get_config, get_mcp_config


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

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(MCP_SERVER_NAME)

QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)
atexit.register(lambda: QUERY_EXECUTOR.shutdown(wait=True))

load_dotenv()

mcp = FastMCP(name=MCP_SERVER_NAME)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    """Health check endpoint for monitoring server status.

    Returns OK if the server is running and can connect to ClickHouse.
    """
    try:
        # Check if ClickHouse is enabled by trying to create config
        # If ClickHouse is disabled, this will succeed but connection will fail
        clickhouse_enabled = os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true"

        if not clickhouse_enabled:
            # If ClickHouse is disabled, check chDB status
            chdb_config = get_chdb_config()
            if chdb_config.enabled:
                return PlainTextResponse("OK - MCP server running with chDB enabled")
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
    client = create_clickhouse_client()
    result = client.command("SHOW DATABASES")

    # Convert newline-separated string to list and trim whitespace
    if isinstance(result, str):
        databases = [db.strip() for db in result.strip().split("\n")]
    else:
        databases = [result]

    logger.info(f"Found {len(databases)} databases")
    return json.dumps(databases)


def list_tables(database: str, like: Optional[str] = None, not_like: Optional[str] = None):
    """List available ClickHouse tables in a database, including schema, comment,
    row count, and column count."""
    logger.info(f"Listing tables in database '{database}'")
    client = create_clickhouse_client()
    query = f"SELECT database, name, engine, create_table_query, dependencies_database, dependencies_table, engine_full, sorting_key, primary_key, total_rows, total_bytes, total_bytes_uncompressed, parts, active_parts, total_marks, comment FROM system.tables WHERE database = {format_query_value(database)}"
    if like:
        query += f" AND name LIKE {format_query_value(like)}"

    if not_like:
        query += f" AND name NOT LIKE {format_query_value(not_like)}"

    result = client.query(query)

    # Deserialize result as Table dataclass instances
    tables = result_to_table(result.column_names, result.result_rows)

    for table in tables:
        column_data_query = f"SELECT database, table, name, type AS column_type, default_kind, default_expression, comment FROM system.columns WHERE database = {format_query_value(database)} AND table = {format_query_value(table.name)}"
        column_data_query_result = client.query(column_data_query)
        table.columns = [
            c
            for c in result_to_column(
                column_data_query_result.column_names,
                column_data_query_result.result_rows,
            )
        ]

    logger.info(f"Found {len(tables)} tables")
    return [asdict(table) for table in tables]


deps = [
    "clickhouse-connect",
    "python-dotenv",
    "uvicorn",
    "pip-system-certs",
    "cachetools",
]

mcp = FastMCP(MCP_SERVER_NAME, dependencies=deps)


# Define types for table information and pagination cache
class TableInfo(TypedDict):
    database: str
    name: str
    comment: Optional[str]
    columns: List[Dict[str, Any]]
    create_table_query: str
    row_count: int
    column_count: int


class PaginationCacheEntry(TypedDict):
    database: str
    like: Optional[str]
    table_names: List[str]
    start_idx: int


# Store pagination state for list_tables with 1-hour expiry
# Using TTLCache from cachetools to automatically expire entries after 1 hour
table_pagination_cache = TTLCache(maxsize=100, ttl=3600)  # 3600 seconds = 1 hour


def get_table_info(
    client,
    database: str,
    table: str,
    table_comments: Dict[str, str],
    column_comments: Dict[str, Dict[str, str]],
) -> TableInfo:
    """Get detailed information about a specific table.

    Args:
        client: ClickHouse client
        database: Database name
        table: Table name
        table_comments: Dictionary of table comments
        column_comments: Dictionary of column comments

    Returns:
        TableInfo object with table details
    """
    logger.info(f"Getting schema info for table {database}.{table}")
    schema_query = f"DESCRIBE TABLE {quote_identifier(database)}.{quote_identifier(table)}"
    schema_result = client.query(schema_query)

    columns = []
    column_names = schema_result.column_names
    for row in schema_result.result_rows:
        column_dict = {}
        for i, col_name in enumerate(column_names):
            column_dict[col_name] = row[i]
        # Add comment from our pre-fetched comments
        if table in column_comments and column_dict["name"] in column_comments[table]:
            column_dict["comment"] = column_comments[table][column_dict["name"]]
        else:
            column_dict["comment"] = None
        columns.append(column_dict)

    # Get row count and column count from the table
    row_count_query = f"SELECT count() FROM {quote_identifier(database)}.{quote_identifier(table)}"
    row_count_result = client.query(row_count_query)
    row_count = row_count_result.result_rows[0][0] if row_count_result.result_rows else 0
    column_count = len(columns)

    create_table_query = f"SHOW CREATE TABLE {database}.`{table}`"
    create_table_result = client.command(create_table_query)

    return {
        "database": database,
        "name": table,
        "comment": table_comments.get(table),
        "columns": columns,
        "create_table_query": create_table_result,
        "row_count": row_count,
        "column_count": column_count,
    }


@mcp.tool()
def list_databases():
    """List available ClickHouse databases"""
    logger.info("Listing all databases")
    client = create_clickhouse_client()
    result = client.command("SHOW DATABASES")
    logger.info(f"Found {len(result) if isinstance(result, list) else 1} databases")
    return result


def fetch_table_names(client, database: str, like: str = None) -> List[str]:
    """Get list of table names from the database.

    Args:
        client: ClickHouse client
        database: Database name
        like: Optional pattern to filter table names

    Returns:
        List of table names
    """
    query = f"SHOW TABLES FROM {quote_identifier(database)}"
    if like:
        query += f" LIKE {format_query_value(like)}"
    result = client.command(query)

    # Convert result to a list of table names
    table_names = []
    if isinstance(result, str):
        # Single table result
        table_names = [t.strip() for t in result.split() if t.strip()]
    elif isinstance(result, Sequence):
        # Multiple table results
        table_names = list(result)

    return table_names


def fetch_table_metadata(
    client, database: str, table_names: List[str]
) -> tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    """Fetch table and column comments for a list of tables.

    Args:
        client: ClickHouse client
        database: Database name
        table_names: List of table names

    Returns:
        Tuple of (table_comments, column_comments)
    """
    if not table_names:
        return {}, {}

    # Get table comments
    table_comments_query = f"SELECT name, comment FROM system.tables WHERE database = {format_query_value(database)} AND name IN ({', '.join(format_query_value(name) for name in table_names)})"
    table_comments_result = client.query(table_comments_query)
    table_comments = {row[0]: row[1] for row in table_comments_result.result_rows}

    # Get column comments
    column_comments_query = f"SELECT table, name, comment FROM system.columns WHERE database = {format_query_value(database)} AND table IN ({', '.join(format_query_value(name) for name in table_names)})"
    column_comments_result = client.query(column_comments_query)
    column_comments = {}
    for row in column_comments_result.result_rows:
        table, col_name, comment = row
        if table not in column_comments:
            column_comments[table] = {}
        column_comments[table][col_name] = comment

    return table_comments, column_comments


def get_paginated_tables(
    client, database: str, table_names: List[str], start_idx: int, page_size: int
) -> tuple[List[TableInfo], int, bool]:
    """Get detailed information for a page of tables.

    Args:
        client: ClickHouse client
        database: Database name
        table_names: List of all table names
        start_idx: Starting index for pagination
        page_size: Number of tables per page

    Returns:
        Tuple of (list of table info, end index, has more pages)
    """
    end_idx = min(start_idx + page_size, len(table_names))
    current_page_table_names = table_names[start_idx:end_idx]

    # Get metadata for current page
    table_comments, column_comments = fetch_table_metadata(
        client, database, current_page_table_names
    )

    # Get detailed information for each table
    tables = []
    for table_name in current_page_table_names:
        tables.append(get_table_info(client, database, table_name, table_comments, column_comments))

    return tables, end_idx, end_idx < len(table_names)


def create_page_token(database: str, like: str, table_names: List[str], end_idx: int) -> str:
    """Create a new page token and store it in the cache.

    Args:
        database: Database name
        like: Pattern used to filter tables
        table_names: List of all table names
        end_idx: Index to start from for the next page

    Returns:
        New page token
    """
    token = str(uuid.uuid4())
    table_pagination_cache[token] = {
        "database": database,
        "like": like,
        "table_names": table_names,
        "start_idx": end_idx,
    }
    return token


@mcp.tool()
def list_tables(database: str, like: str = None, page_token: str = None, page_size: int = 50):
    """List available ClickHouse tables in a database, including schema, comment,
    row count, and column count.

    Args:
        database: The database to list tables from
        like: Optional pattern to filter table names
        page_token: Token for pagination, obtained from a previous call
        page_size: Number of tables to return per page (default: 50)

    Returns:
        A dictionary containing:
        - tables: List of table information
        - next_page_token: Token for the next page, or None if no more pages
    """
    logger.info(
        f"Listing tables in database '{database}' with page_token={page_token}, page_size={page_size}"
    )
    client = create_clickhouse_client()

    # If we have a page token, retrieve the cached state
    if page_token and page_token in table_pagination_cache:
        cached_state = table_pagination_cache[page_token]
        if cached_state["database"] != database or cached_state["like"] != like:
            logger.warning(f"Page token {page_token} is for a different database or filter")
            page_token = None
        else:
            # Use the cached state
            table_names = cached_state["table_names"]
            start_idx = cached_state["start_idx"]

            # Get tables for current page
            tables, end_idx, has_more = get_paginated_tables(
                client, database, table_names, start_idx, page_size
            )

            # Generate next page token if there are more tables
            next_page_token = None
            if has_more:
                next_page_token = create_page_token(database, like, table_names, end_idx)

            # Clean up the used page token
            del table_pagination_cache[page_token]

            return {"tables": tables, "next_page_token": next_page_token}

    # If no valid page token, fetch all table names
    table_names = fetch_table_names(client, database, like)

    # Apply pagination
    start_idx = 0
    tables, end_idx, has_more = get_paginated_tables(
        client, database, table_names, start_idx, page_size
    )

    # Generate next page token if there are more tables
    next_page_token = None
    if has_more:
        next_page_token = create_page_token(database, like, table_names, end_idx)

    logger.info(
        f"Found {len(table_names)} tables, returning {len(tables)} with next_page_token={next_page_token}"
    )
    return {"tables": tables, "next_page_token": next_page_token}


def execute_query(query: str):
    client = create_clickhouse_client()
    try:
        read_only = get_readonly_setting(client)
        res = client.query(query, settings={"readonly": read_only})
        logger.info(f"Query returned {len(res.result_rows)} rows")
        return {"columns": res.column_names, "rows": res.result_rows}
    except Exception as err:
        logger.error(f"Error executing query: {err}")
        raise ToolError(f"Query execution failed: {str(err)}")


def run_select_query(query: str):
    """Run a SELECT query in a ClickHouse database"""
    logger.info(f"Executing SELECT query: {query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_query, query)
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
            logger.warning(f"Query timed out after {timeout_secs} seconds: {query}")
            future.cancel()
            raise ToolError(f"Query timed out after {timeout_secs} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in run_select_query: {str(e)}")
        raise RuntimeError(f"Unexpected error during query execution: {str(e)}")


def create_clickhouse_client():
    client_config = get_config().get_client_config()
    logger.info(
        f"Creating ClickHouse client connection to {client_config['host']}:{client_config['port']} "
        f"as {client_config['username']} "
        f"(secure={client_config['secure']}, verify={client_config['verify']}, "
        f"connect_timeout={client_config['connect_timeout']}s, "
        f"send_receive_timeout={client_config['send_receive_timeout']}s)"
    )

    try:
        client = clickhouse_connect.get_client(**client_config)
        # Test the connection
        version = client.server_version
        logger.info(f"Successfully connected to ClickHouse server version {version}")
        return client
    except Exception as e:
        logger.error(f"Failed to connect to ClickHouse: {str(e)}")
        raise


def get_readonly_setting(client) -> str:
    """Get the appropriate readonly setting value to use for queries.

    This function handles potential conflicts between server and client readonly settings:
    - readonly=0: No read-only restrictions
    - readonly=1: Only read queries allowed, settings cannot be changed
    - readonly=2: Only read queries allowed, settings can be changed (except readonly itself)

    If server has readonly=2 and client tries to set readonly=1, it would cause:
    "Setting readonly is unknown or readonly" error

    This function preserves the server's readonly setting unless it's 0, in which case
    we enforce readonly=1 to ensure queries are read-only.

    Args:
        client: ClickHouse client connection

    Returns:
        String value of readonly setting to use
    """
    read_only = client.server_settings.get("readonly")
    if read_only:
        if read_only == "0":
            return "1"  # Force read-only mode if server has it disabled
        else:
            return read_only.value  # Respect server's readonly setting (likely 2)
    else:
        return "1"  # Default to basic read-only mode if setting isn't present


def create_chdb_client():
    """Create a chDB client connection."""
    if not get_chdb_config().enabled:
        raise ValueError("chDB is not enabled. Set CHDB_ENABLED=true to enable it.")
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
    try:
        if not get_chdb_config().enabled:
            logger.info("chDB is disabled, skipping client initialization")
            return None

        client_config = get_chdb_config().get_client_config()
        data_path = client_config["data_path"]
        logger.info(f"Creating chDB client with data_path={data_path}")
        client = chs.Session(path=data_path)
        logger.info(f"Successfully connected to chDB with data_path={data_path}")
        return client
    except Exception as e:
        logger.error(f"Failed to initialize chDB client: {e}")
        return None


# Register tools based on configuration
if os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true":
    mcp.add_tool(Tool.from_function(list_databases))
    mcp.add_tool(Tool.from_function(list_tables))
    mcp.add_tool(Tool.from_function(run_select_query))
    logger.info("ClickHouse tools registered")


if os.getenv("CHDB_ENABLED", "false").lower() == "true":
    _chdb_client = _init_chdb_client()
    if _chdb_client:
        atexit.register(lambda: _chdb_client.close())

    mcp.add_tool(Tool.from_function(run_chdb_select_query))
    chdb_prompt = Prompt.from_function(
        chdb_initial_prompt,
        name="chdb_initial_prompt",
        description="This prompt helps users understand how to interact and perform common operations in chDB",
    )
    mcp.add_prompt(chdb_prompt)
    logger.info("chDB tools and prompts registered")
