import logging
import json
from typing import Optional, List, Any, Dict
import concurrent.futures
import atexit
import os

import clickhouse_connect
import chdb.session as chs
from clickhouse_connect.driver.binding import format_query_value
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.tools import Tool
from fastmcp.prompts import Prompt
from fastmcp.exceptions import ToolError
from dataclasses import dataclass, field, asdict, is_dataclass
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_clickhouse.mcp_env import get_config, get_all_configs, get_mcp_server_config, get_chdb_config
from mcp_clickhouse.chdb_prompt import CHDB_PROMPT


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


def list_clickhouse_servers():
    """List all available ClickHouse server configurations."""
    logger.info("Listing all configured ClickHouse servers")
    try:
        servers = get_all_configs().get_available_servers()
        return servers
    except Exception as e:
        logger.error(f"Error listing servers: {str(e)}")
        return {"error": str(e)}


def list_databases(clickhouse_server: Optional[str] = None):
    """List available ClickHouse databases.

    Args:
        clickhouse_server: Optional ClickHouse server name, uses default if not specified
    """
    logger.info(f"Listing all databases from server: {clickhouse_server or 'default'}")
    try:
        if not clickhouse_server:
            available_servers = get_all_configs().get_available_servers()
            if not available_servers:
                return {"error": "No valid ClickHouse configurations available"}

        client = create_clickhouse_client(clickhouse_server)
        result = client.command("SHOW DATABASES")

        # Convert newline-separated string to list and trim whitespace
        if isinstance(result, str):
            databases = [db.strip() for db in result.strip().split("\n")]
        else:
            databases = [result]

        logger.info(f"Found {len(databases)} databases")
        return json.dumps(databases)
    except Exception as e:
        logger.error(f"Error listing databases: {str(e)}")
        return {"error": str(e)}


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
            # Get readonly setting
            read_only = get_readonly_setting(client)
            res = client.query(query, settings={"readonly": read_only})
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
            logger.warning(f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds: {query}")
            future.cancel()
            raise ToolError(f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in run_select_query: {str(e)}")
        # Return structured error instead of raising to prevent MCP serialization failures
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}


def get_readonly_setting(client):
    """Get the appropriate readonly setting for the client."""
    try:
        read_only = client.server_settings.get("readonly")
        if read_only:
            if read_only == "0":
                return "1"  # Force read-only mode if server has it disabled
            else:
                return read_only  # Respect server's readonly setting (likely 2)
        else:
            return "1"  # Default to basic read-only mode if setting isn't present
    except:
        return "1"  # Default to basic read-only mode if we can't get server settings


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
            result = future.result(timeout=SELECT_QUERY_TIMEOUT_SECS)
            # Check if we received an error structure from execute_chdb_query
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"chDB query failed: {result['error']}")
                return {
                    "status": "error",
                    "message": f"chDB query failed: {result['error']}",
                }
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(
                f"chDB query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds: {query}"
            )
            future.cancel()
            return {
                "status": "error",
                "message": f"chDB query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds",
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
    mcp.add_tool(Tool.from_function(list_clickhouse_servers))
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


def run_server():
    """Start the MCP server with the configured transport and port settings."""
    server_config = get_mcp_server_config()
    logger.info(f"Starting MCP server on {server_config.host}:{server_config.port} with streamable-http transport")

    # Use streamable-http transport to listen for HTTP requests
    mcp.run(transport="streamable-http")

