import logging
from typing import Sequence, Dict, Any, Optional, List, TypedDict
import concurrent.futures
import atexit
import uuid

import clickhouse_connect
from clickhouse_connect.driver.binding import quote_identifier, format_query_value
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from cachetools import TTLCache

from mcp_clickhouse.mcp_env import get_config

MCP_SERVER_NAME = "mcp-clickhouse"

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(MCP_SERVER_NAME)

QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)
atexit.register(lambda: QUERY_EXECUTOR.shutdown(wait=True))
SELECT_QUERY_TIMEOUT_SECS = 30

load_dotenv()

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
        res = client.query(query, settings={"readonly": 1})
        column_names = res.column_names
        rows = []
        for row in res.result_rows:
            row_dict = {}
            for i, col_name in enumerate(column_names):
                row_dict[col_name] = row[i]
            rows.append(row_dict)
        logger.info(f"Query returned {len(rows)} rows")
        return rows
    except Exception as err:
        logger.error(f"Error executing query: {err}")
        # Return a structured dictionary rather than a string to ensure proper serialization
        # by the MCP protocol. String responses for errors can cause BrokenResourceError.
        return {"error": str(err)}


@mcp.tool()
def run_select_query(query: str):
    """Run a SELECT query in a ClickHouse database"""
    logger.info(f"Executing SELECT query: {query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_query, query)
        try:
            result = future.result(timeout=SELECT_QUERY_TIMEOUT_SECS)
            # Check if we received an error structure from execute_query
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"Query failed: {result['error']}")
                # MCP requires structured responses; string error messages can cause
                # serialization issues leading to BrokenResourceError
                return {"status": "error", "message": f"Query failed: {result['error']}"}
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds: {query}")
            future.cancel()
            # Return a properly structured response for timeout errors
            return {
                "status": "error",
                "message": f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds",
            }
    except Exception as e:
        logger.error(f"Unexpected error in run_select_query: {str(e)}")
        # Catch all other exceptions and return them in a structured format
        # to prevent MCP serialization failures
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}


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
