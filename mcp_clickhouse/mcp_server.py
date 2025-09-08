import logging
import json
from typing import Optional, List, Any
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

from mcp_clickhouse.mcp_env import get_config, get_chdb_config
from mcp_clickhouse.chdb_prompt import CHDB_PROMPT


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
SELECT_QUERY_TIMEOUT_SECS = 30

load_dotenv()

mcp = FastMCP(
    name=MCP_SERVER_NAME,
    dependencies=[
        "clickhouse-connect",
        "python-dotenv",
        "pip-system-certs",
        "chdb",
    ],
)


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
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds: {query}")
            future.cancel()
            raise ToolError(f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds")
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


def execute_write_query(query: str):
    """This function bypasses the read-only mode and allows write queries to be executed.

    TODO: Find a sustainable way to execute write queries.
    
    Args:
        query: The write query to execute
    
    Returns:
        The result of the write query
    """
    client = create_clickhouse_client()
    try:
        res = client.command(query)
        logger.info("Write query executed successfully")
        return res
    except Exception as err:
        logger.error(f"Error executing write query: {err}")
        return {"error": str(err)}


def save_memory(key: str, value: str):
    """Store user-provided information as key-value pairs for later retrieval and reference. Generate a concise, descriptive key that summarizes the value content provided."""
    logger.info(f"Saving memory with key: {key}")
    
    try:
        # Create table if it doesn't exist
        logger.info("Ensuring user_memory table exists")
        create_table_query = """
        CREATE TABLE IF NOT EXISTS user_memory (
            key String,
            value String,
            created_at DateTime DEFAULT now(),
            updated_at DateTime DEFAULT now()
        ) ENGINE = MergeTree()
        ORDER BY key
        """
        
        create_result = execute_write_query(create_table_query)
        if isinstance(create_result, dict) and "error" in create_result:
            return {
                "status": "error",
                "message": f"Failed to create user_memory table: {create_result['error']}"
            }
        
        # Insert or replace the memory data using REPLACE INTO for upsert behavior
        insert_query = f"""
        INSERT INTO user_memory (key, value, updated_at) 
        VALUES ({format_query_value(key)}, {format_query_value(value)}, now())
        """
        
        insert_result = execute_write_query(insert_query)
        if isinstance(insert_result, dict) and "error" in insert_result:
            return {
                "status": "error", 
                "message": f"Failed to save memory: {insert_result['error']}"
            }
        
        logger.info(f"Successfully saved memory with key: {key}")
        return {
            "status": "success",
            "message": f"Memory '{key}' saved successfully",
            "key": key
        }
        
    except Exception as e:
        logger.error(f"Unexpected error in save_memory: {str(e)}")
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}


def get_memories_titles():
    """Retrieve all memory keys/titles from the user memory table to see what information has been stored."""
    logger.info("Retrieving all memory titles")
    
    try:
        # Query to get all keys with their timestamps
        query = """
        SELECT key, created_at, updated_at 
        FROM user_memory 
        ORDER BY updated_at DESC
        """
        
        result = execute_query(query)
        
        # Check if we received an error structure from execute_query
        if isinstance(result, dict) and "error" in result:
            return {
                "status": "error",
                "message": f"Failed to retrieve memory titles: {result['error']}"
            }
        
        # Extract just the keys for the response from the new result format
        rows = result.get("rows", [])
        titles = [row[0] for row in rows] if rows else []
        
        # Convert rows to dict format for details
        columns = result.get("columns", [])
        details = []
        for row in rows:
            row_dict = {}
            for i, col_name in enumerate(columns):
                row_dict[col_name] = row[i]
            details.append(row_dict)
        
        logger.info(f"Retrieved {len(titles)} memory titles")
        return {
            "status": "success",
            "titles": titles,
            "count": len(titles),
            "details": details  # Include full details with timestamps
        }
        
    except Exception as e:
        logger.error(f"Unexpected error in get_memories_titles: {str(e)}")
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}


def get_memory(key: str):
    """Retrieve all memory entries matching the specified key from the user memory table."""
    logger.info(f"Retrieving memory for key: {key}")
    
    try:
        # Query to get all memories matching the key, ordered by most recent first
        query = f"""
        SELECT key, value, created_at, updated_at 
        FROM user_memory 
        WHERE key = {format_query_value(key)}
        ORDER BY updated_at DESC
        """
        
        result = execute_query(query)
        
        # Check if we received an error structure from execute_query
        if isinstance(result, dict) and "error" in result:
            return {
                "status": "error",
                "message": f"Failed to retrieve memory: {result['error']}"
            }
        
        # Convert to dict format
        columns = result.get("columns", [])
        rows = result.get("rows", [])
        memories = []
        for row in rows:
            row_dict = {}
            for i, col_name in enumerate(columns):
                row_dict[col_name] = row[i]
            memories.append(row_dict)
        
        # Check if memory exists
        if not memories:
            logger.info(f"No memory found for key: {key}")
            return {
                "status": "not_found",
                "message": f"No memory found with key '{key}'",
                "key": key
            }
        
        # Return all matching memories
        logger.info(f"Successfully retrieved {len(memories)} memories for key: {key}")
        
        return {
            "status": "success",
            "key": key,
            "count": len(memories),
            "memories": memories
        }
        
    except Exception as e:
        logger.error(f"Unexpected error in get_memory: {str(e)}")
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}


def get_all_memories():
    """Retrieve all saved memories from the user memory table, don't list them back, just the give the number of memories retrieved. WARNING: This tool should only be used when explicitly requested by the user, as it may return large amounts of data."""
    logger.info("Retrieving all memories")
    
    try:
        # Query to get all memories ordered by most recent first
        query = """
        SELECT key, value, created_at, updated_at 
        FROM user_memory 
        ORDER BY updated_at DESC
        """
        
        result = execute_query(query)
        
        # Check if we received an error structure from execute_query
        if isinstance(result, dict) and "error" in result:
            return {
                "status": "error",
                "message": f"Failed to retrieve all memories: {result['error']}"
            }
        
        # Convert to dict format
        columns = result.get("columns", [])
        rows = result.get("rows", [])
        memories = []
        for row in rows:
            row_dict = {}
            for i, col_name in enumerate(columns):
                row_dict[col_name] = row[i]
            memories.append(row_dict)
        
        # Return all memories
        logger.info(f"Successfully retrieved {len(memories)} total memories")
        
        return {
            "status": "success",
            "count": len(memories),
            "memories": memories
        }
        
    except Exception as e:
        logger.error(f"Unexpected error in get_all_memories: {str(e)}")
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}


def delete_memory(key: str):
    """Delete all memory entries matching the specified key from the user memory table. Warining this tool should only be used when explicitly requested by the user"""
    logger.info(f"Deleting memory for key: {key}")
    
    try:
        # First check if memories exist for this key
        check_query = f"""
        SELECT count() 
        FROM user_memory 
        WHERE key = {format_query_value(key)}
        """
        
        check_result = execute_query(check_query)
        
        # Check if we received an error structure from execute_query
        if isinstance(check_result, dict) and "error" in check_result:
            return {
                "status": "error",
                "message": f"Failed to check memory existence: {check_result['error']}"
            }
        
        # Check if any memories exist - handle new result format
        rows = check_result.get("rows", [])
        if not rows or len(rows) == 0 or rows[0][0] == 0:
            logger.info(f"No memories found for key: {key}")
            return {
                "status": "not_found",
                "message": f"No memories found with key '{key}'",
                "key": key
            }
        
        memories_count = rows[0][0]
        
        # Delete the memories
        delete_query = f"""
        ALTER TABLE user_memory 
        DELETE WHERE key = {format_query_value(key)}
        """
        
        delete_result = execute_write_query(delete_query)
        if isinstance(delete_result, dict) and "error" in delete_result:
            return {
                "status": "error", 
                "message": f"Failed to delete memories: {delete_result['error']}"
            }
        
        logger.info(f"Successfully deleted {memories_count} memories for key: {key}")
        return {
            "status": "success",
            "message": f"Deleted {memories_count} memory entries with key '{key}'",
            "key": key,
            "deleted_count": memories_count
        }
        
    except Exception as e:
        logger.error(f"Unexpected error in delete_memory: {str(e)}")
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}


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

# Conditionally register memory tools based on CLICKHOUSE_MEMORY flag
config = get_config()
if config.memory_enabled:
    logger.info("Memory tools enabled - registering memory management tools")
    mcp.add_tool(Tool.from_function(save_memory))
    mcp.add_tool(Tool.from_function(get_memories_titles))
    mcp.add_tool(Tool.from_function(get_memory))
    mcp.add_tool(Tool.from_function(get_all_memories))
    mcp.add_tool(Tool.from_function(delete_memory))
else:
    logger.info("Memory tools disabled - set CLICKHOUSE_MEMORY=true to enable")
