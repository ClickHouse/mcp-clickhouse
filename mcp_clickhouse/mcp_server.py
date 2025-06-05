"""MCP server for ClickHouse integration.

This module provides an MCP (Machine Communication Protocol) server that integrates
with ClickHouse, allowing AI agents to query and interact with ClickHouse
databases. It supports multiple server connections and provides tools for
listing databases, tables, and executing read-only queries.
"""

import logging
import json
from typing import Optional, List, Any, Dict
import concurrent.futures
import atexit

import clickhouse_connect
from clickhouse_connect.driver.binding import format_query_value
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from dataclasses import dataclass, field, asdict, is_dataclass

from mcp_clickhouse.mcp_env import get_config, get_all_configs, get_mcp_server_config


@dataclass
class Column:
    """ClickHouse column metadata."""
    database: str
    table: str
    name: str
    column_type: str
    default_kind: Optional[str]
    default_expression: Optional[str]
    comment: Optional[str]


@dataclass
class Table:
    """ClickHouse table metadata."""
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

# Initialize query executor thread pool
QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)
atexit.register(lambda: QUERY_EXECUTOR.shutdown(wait=True))
SELECT_QUERY_TIMEOUT_SECS = 30

# Load environment variables
load_dotenv()

# MCP dependencies
deps = [
    "clickhouse-connect",
    "python-dotenv",
    "uvicorn",
    "pip-system-certs",
]

# Get server configuration
server_config = get_mcp_server_config()
port = server_config.port
host = server_config.host

# Create FastMCP instance
mcp = FastMCP(MCP_SERVER_NAME, dependencies=deps, port=port, host=host)


def result_to_table(query_columns, result) -> List[Table]:
    """Convert query result to Table objects.

    Args:
        query_columns: Column names from the query
        result: Query result rows

    Returns:
        List of Table objects
    """
    return [Table(**dict(zip(query_columns, row))) for row in result]


def result_to_column(query_columns, result) -> List[Column]:
    """Convert query result to Column objects.

    Args:
        query_columns: Column names from the query
        result: Query result rows

    Returns:
        List of Column objects
    """
    return [Column(**dict(zip(query_columns, row))) for row in result]


def to_json(obj: Any) -> Any:
    """Convert dataclasses to JSON-serializable objects.

    Args:
        obj: Object to convert

    Returns:
        JSON-serializable version of the object
    """
    if is_dataclass(obj):
        return json.dumps(asdict(obj), default=to_json)
    elif isinstance(obj, list):
        return [to_json(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: to_json(value) for key, value in obj.items()}
    return obj


@mcp.tool()
def list_clickhouse_servers():
    """List all available ClickHouse server configurations."""
    logger.info("Listing all configured ClickHouse servers")
    try:
        servers = get_all_configs().get_available_servers()
        return servers
    except Exception as e:
        logger.error(f"Error listing servers: {str(e)}")
        return {"error": str(e)}


@mcp.tool()
def list_databases(clickhouse_server: Optional[str] = None):
    """List available ClickHouse databases.

    Args:
        clickhouse_server: Optional ClickHouse server name, uses default if not specified
    """
    logger.info(f"Listing all databases from server: {clickhouse_server or 'default'}")
    try:
        # 确保有一个有效的服务器配置
        if not clickhouse_server:
            available_servers = get_all_configs().get_available_servers()
            if not available_servers:
                return {"error": "No valid ClickHouse configurations available"}

        client = create_clickhouse_client(clickhouse_server)
        result = client.command("SHOW DATABASES")
        logger.info(f"Found {len(result) if isinstance(result, list) else 1} databases")
        return result
    except Exception as e:
        logger.error(f"Error listing databases: {str(e)}")
        return {"error": str(e)}


@mcp.tool()
def list_tables(
    database: str,
    like: Optional[str] = None,
    not_like: Optional[str] = None,
    clickhouse_server: Optional[str] = None
):
    """List available ClickHouse tables in a database, including schema, comment,
    row count, and column count.

    Args:
        database: Database name
        like: Optional table name pattern to match
        not_like: Optional table name pattern to exclude
        clickhouse_server: Optional ClickHouse server name, uses default if not specified
    """
    logger.info(f"Listing tables in database '{database}' from server: {clickhouse_server or 'default'}")
    try:
        client = create_clickhouse_client(clickhouse_server)
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
    except Exception as e:
        logger.error(f"Error listing tables: {str(e)}")
        return {"error": str(e)}


def execute_query(query: str, clickhouse_server: Optional[str] = None):
    """Execute a query on the ClickHouse server.

    Args:
        query: SQL query to execute
        clickhouse_server: Optional server name

    Returns:
        Query results or error dictionary
    """
    try:
        query_upper = query.upper().strip()

        allowed_prefixes = [
            "SELECT ",
            "SHOW ",
            "DESCRIBE ",
            "DESC ",
            "EXISTS ",
            "EXPLAIN "
        ]

        forbidden_keywords = [
            "INSERT",
            "UPDATE",
            "DELETE",
            "DROP",
            "CREATE",
            "ALTER",
            "RENAME",
            "TRUNCATE",
            "OPTIMIZE",
            "KILL",
            "ATTACH",
            "DETACH",
            "SYSTEM",
            "GRANT",
            "REVOKE",
            "SET "
        ]

        is_allowed = any(query_upper.startswith(prefix) for prefix in allowed_prefixes)

        contains_forbidden = any(f" {keyword} " in f" {query_upper} " or query_upper.startswith(keyword) for keyword in forbidden_keywords)

        if not is_allowed or contains_forbidden:
            logger.warning(f"Rejected non-read-only query: {query}")
            return {
                "error": "Only read-only queries (SELECT, SHOW, DESCRIBE, etc.) are allowed for security reasons."
            }

        client = create_clickhouse_client(clickhouse_server)

        # Check for server name prefix in query and clean it
        if clickhouse_server and f"{clickhouse_server}." in query:
            query = query.replace(f"{clickhouse_server}.", "")
            logger.info(f"Removed server name prefix from query")

        try:
            # Force readonly mode regardless of server setting
            # Setting readonly=1 ensures only read queries are allowed
            res = client.query(query, settings={"readonly": "1"})
            column_names = res.column_names
            rows = []
            for row in res.result_rows:
                row_dict = {}
                for i, col_name in enumerate(column_names):
                    # Handle special data types for serialization
                    value = row[i]
                    if hasattr(value, 'isoformat'):  # For datetime objects
                        value = value.isoformat()
                    row_dict[col_name] = value
                rows.append(row_dict)
            logger.info(f"Query returned {len(rows)} rows")
            return rows
        except Exception as err:
            logger.error(f"Error executing query: {err}")
            # Return a structured dictionary rather than a string to ensure proper serialization
            # by the MCP protocol. String responses for errors can cause BrokenResourceError.
            return {"error": str(err)}
    except Exception as e:
        logger.error(f"Failed to create client: {e}")
        return {"error": f"Connection error: {str(e)}"}


@mcp.tool()
def run_select_query(query: str, clickhouse_server: Optional[str] = None):
    """Run a SELECT query in a ClickHouse database.

    Args:
        query: SQL query to execute
        clickhouse_server: Optional ClickHouse server name, uses default if not specified
    """
    logger.info(f"Executing SELECT query on server '{clickhouse_server or 'default'}': {query}")
    try:
        # Auto-correct query if it contains server name as database prefix
        if clickhouse_server and f"{clickhouse_server}." in query:
            query = query.replace(f"{clickhouse_server}.", "")
            logger.info(f"Modified query to remove server prefix")

        future = QUERY_EXECUTOR.submit(execute_query, query, clickhouse_server)
        try:
            result = future.result(timeout=SELECT_QUERY_TIMEOUT_SECS)
            # Check if we received an error structure from execute_query
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"Query failed: {result['error']}")
                # MCP requires structured responses; string error messages can cause
                # serialization issues leading to BrokenResourceError
                return {
                    "status": "error",
                    "message": f"Query failed: {result['error']}",
                }

            # Ensure result is serializable
            try:
                json.dumps(result)
            except (TypeError, OverflowError) as e:
                logger.error(f"Query result contains non-serializable data: {e}")
                return {
                    "status": "error",
                    "message": f"Result format error: {e}",
                }

            return result
        except concurrent.futures.TimeoutError:
            logger.warning(
                f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds: {query}"
            )
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


def create_clickhouse_client(server_name: Optional[str] = None):
    """Create a ClickHouse client connection.

    Args:
        server_name: Optional server name, uses default if not specified

    Returns:
        ClickHouse client instance
    """
    client_config = get_config(server_name).get_client_config()
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


@mcp.tool()
def diagnose_connection(clickhouse_server: Optional[str] = None):
    """Diagnose ClickHouse connection issues.

    This tool runs a series of checks to identify connection or permission issues.

    Args:
        clickhouse_server: Optional ClickHouse server name to diagnose

    Returns:
        Diagnostic information dictionary
    """
    try:
        # Check configuration
        config = get_config(clickhouse_server)
        client_config = config.get_client_config()
        connection_info = {
            "host": client_config["host"],
            "port": client_config["port"],
            "secure": client_config["secure"],
            "database": client_config.get("database", "Not specified"),
        }

        # Try connecting
        try:
            client = create_clickhouse_client(clickhouse_server)
            version = client.server_version

            # Try simple query
            try:
                result = client.command("SELECT 1")

                # Check system tables access
                try:
                    tables_result = client.command("SELECT count() FROM system.tables")

                    return {
                        "status": "success",
                        "message": "All checks passed",
                        "connection": connection_info,
                        "version": version,
                        "system_tables_count": tables_result[0][0] if isinstance(tables_result, list) and len(tables_result) > 0 else "Unknown"
                    }
                except Exception as e:
                    return {
                        "status": "warning",
                        "message": f"Basic connection works but system tables access restricted: {str(e)}",
                        "connection": connection_info,
                        "version": version
                    }

            except Exception as e:
                return {
                    "status": "error",
                    "message": f"Connection successful but query failed: {str(e)}",
                    "connection": connection_info,
                    "version": version,
                }

        except Exception as e:
            return {
                "status": "error",
                "message": f"Cannot connect to server: {str(e)}",
                "connection": connection_info,
            }

    except Exception as e:
        return {
            "status": "error",
            "message": f"Configuration error: {str(e)}",
        }


def run_server():
    """Start the MCP server with the configured transport and port settings."""
    server_config = get_mcp_server_config()
    logger.info(f"Starting MCP server on {server_config.host}:{server_config.port} with streamable-http transport")

    # Use streamable-http transport to listen for HTTP requests
    mcp.run(transport="streamable-http")
