import logging
import json
from typing import Optional, List, Any
import concurrent.futures
import atexit

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

from mcp_clickhouse.mcp_env import load_clickhouse_configs, load_chdb_configs, list_clickhouse_tenants, list_chdb_tenants, get_config, get_chdb_config
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

# Load Configs
load_dotenv()
load_clickhouse_configs()
load_chdb_configs()

# List of Tenants
CLICKHOUSE_TENANTS = list_clickhouse_tenants()
CHDB_TENANTS = list_chdb_tenants()

# Create ThreadPoolExecutors for each tenant
CLICKHOUSE_QUERY_EXECUTOR = {
    tenant: concurrent.futures.ThreadPoolExecutor(max_workers=10)
    for tenant in CLICKHOUSE_TENANTS
}

CHDB_QUERY_EXECUTOR = {
    tenant: concurrent.futures.ThreadPoolExecutor(max_workers=10)
    for tenant in CHDB_TENANTS
}

# Ensure all executors are properly shutdown on exit
atexit.register(lambda: [executor.shutdown(wait=True) for executor in CLICKHOUSE_QUERY_EXECUTOR.values()])
atexit.register(lambda: [executor.shutdown(wait=True) for executor in CHDB_QUERY_EXECUTOR.values()])

# Default query timeout for selects
SELECT_QUERY_TIMEOUT_SECS = 30

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
        reports = []

        for tenant in CLICKHOUSE_TENANTS:
            tenant_report = f"Tenant - '{tenant}': "

            # Check if ClickHouse is enabled by trying to create config 
            # If ClickHouse is disabled, this will succeed but connection will fail
            try:
                clickhouse_config = get_config(tenant)
                if clickhouse_config.enabled:
                    client = create_clickhouse_client(tenant)
                    version = client.server_version
                    tenant_report += f"ClickHouse OK (v{version})"
                else:
                    tenant_report += "ClickHouse Disabled"
            except Exception as e:
                tenant_report += f"ClickHouse ERROR ({str(e)})"

            # Check chDB status if enabled
            try:
                chdb_config = get_chdb_config(tenant)
                if chdb_config.enabled:
                    tenant_report += ", chDB OK"
                else:
                    tenant_report += ", chDB Disabled"
            except Exception as e:
                tenant_report += f", chDB ERROR ({str(e)})"

            reports.append(tenant_report)

        return PlainTextResponse("\n".join(reports))

    except Exception as e:
        return PlainTextResponse(f"ERROR - Health check failed: {str(e)}", status_code=503)


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

def clickhouse_tenant_available(tenant: str):
    if tenant in CLICKHOUSE_TENANTS:
        return True
    return False

def chdb_tenant_available(tenant: str):
    if tenant in CHDB_TENANTS:
        return True
    return False

def list_databases(tenant: str):
    """List available ClickHouse databases"""
    logger.info("Listing all databases")

    if not clickhouse_tenant_available(tenant):
        logger.warning(f"List databases not performed for non-existent tenant - '{tenant}'")
        return {
            "status": "error",
            "message": f"List databases not performed for non-existent tenant - '{tenant}'"
        }

    client = create_clickhouse_client(tenant)
    result = client.command("SHOW DATABASES")

    # Convert newline-separated string to list and trim whitespace
    if isinstance(result, str):
        databases = [db.strip() for db in result.strip().split("\n")]
    else:
        databases = [result]

    logger.info(f"Found {len(databases)} databases for tenant - '{tenant}'")
    return json.dumps(databases)


def list_tables(tenant: str, database: str, like: Optional[str] = None, not_like: Optional[str] = None):
    """List available ClickHouse tables in a database, including schema, comment,
    row count, and column count."""

    if not clickhouse_tenant_available(tenant):
        logger.warning(f"List tables not performed for non-existent tenant - '{tenant}'")
        return {
            "status": "error",
            "message": f"List tables not performed for non-existent tenant - '{tenant}'"
        }

    logger.info(f"Listing tables for tenant - '{tenant}' in database '{database}'")
    client = create_clickhouse_client(tenant)
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

    logger.info(f"Found {len(tables)} tables for tenant - '{tenant}'")
    return [asdict(table) for table in tables]


def execute_query(tenant: str, query: str):
    client = create_clickhouse_client(tenant)
    try:
        read_only = get_readonly_setting(client)
        res = client.query(query, settings={"readonly": read_only})
        logger.info(f"Query returned {len(res.result_rows)} rows")
        return {"columns": res.column_names, "rows": res.result_rows}
    except Exception as err:
        logger.error(f"Error executing query: {err}")
        raise ToolError(f"Query execution failed: {str(err)}")


def run_select_query(tenant: str, query: str):
    """Run a SELECT query in a ClickHouse database"""
    if not clickhouse_tenant_available(tenant):
        logger.warning(f"Query not performed for non-existent tenant - '{tenant}'")
        return {
            "status": "error",
            "message": f"Query not performed for non-existent tenant - '{tenant}'"
        }
        
    logger.info(f"Executing SELECT query for tenant - '{tenant}': {query}")
    try:
        future = CLICKHOUSE_QUERY_EXECUTOR[tenant].submit(execute_query, tenant, query)
        try:
            result = future.result(timeout=SELECT_QUERY_TIMEOUT_SECS)
            # Check if we received an error structure from execute_query
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"Query failed for tenant - '{tenant}': {result['error']}")
                # MCP requires structured responses; string error messages can cause
                # serialization issues leading to BrokenResourceError
                return {
                    "status": "error",
                    "message": f"Query failed for tenant - '{tenant}': {result['error']}",
                }
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds for tenant - '{tenant}': {query}")
            future.cancel()
            raise ToolError(f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds for tenant - '{tenant}'")
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in run_select_query for tenant - '{tenant}': {str(e)}")
        raise RuntimeError(f"Unexpected error during query execution for tenant - '{tenant}': {str(e)}")


def create_clickhouse_client(tenant: str):
    if not clickhouse_tenant_available(tenant):
        logger.warning(f"Clickhouse client not created for non-existent tenant - '{tenant}'")
        return {
            "status": "error",
            "message": f"Clickhouse client not created for non-existent tenant - '{tenant}'"
        }
    
    client_config = get_config(tenant).get_client_config()
    logger.info(
        f"Creating ClickHouse client connection for tenant - '{tenant}', to {client_config['host']}:{client_config['port']} "
        f"as {client_config['username']} "
        f"(secure={client_config['secure']}, verify={client_config['verify']}, "
        f"connect_timeout={client_config['connect_timeout']}s, "
        f"send_receive_timeout={client_config['send_receive_timeout']}s)"
    )

    try:
        client = clickhouse_connect.get_client(**client_config)
        # Test the connection
        version = client.server_version
        logger.info(f"Successfully connected to ClickHouse server version {version} for tenant - '{tenant}'")
        return client
    except Exception as e:
        logger.error(f"Failed to connect to ClickHouse for tenant - '{tenant}': {str(e)}")
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


def create_chdb_client(tenant: str):
    """Create a chDB client connection."""
    if not chdb_tenant_available(tenant):
        logger.warning(f"chDB client not created for non-existent tenant - '{tenant}'")
        return {
            "status": "error",
            "message": f"chDB client not created for non-existent tenant - '{tenant}'"
        }
    
    if not get_chdb_config(tenant).enabled:
        raise ValueError(f"chDB is not enabled for tenant - '{tenant}'. Set CHDB_ENABLED=true to enable it.")
    return _chdb_client


def execute_chdb_query(tenant: str, query: str):
    """Execute a query using chDB client."""
    if not chdb_tenant_available(tenant):
        logger.warning(f"chDB Query not performed for non-existent tenant - '{tenant}'")
        return {
            "status": "error",
            "message": f"chDB Query not performed for non-existent tenant - '{tenant}'"
        }

    client = create_chdb_client(tenant)
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


def run_chdb_select_query(tenant: str, query: str):
    """Run SQL in chDB, an in-process ClickHouse engine"""
    if not chdb_tenant_available(tenant):
        logger.warning(f"chDB query not performed for non-existent tenant - '{tenant}'")
        return {
            "status": "error",
            "message": f"chDB query not performed for non-existent tenant - '{tenant}'"
        }
        
    logger.info(f"Executing chDB SELECT query for tenant - '{tenant}': {query}")
    try:
        future = CHDB_QUERY_EXECUTOR[tenant].submit(execute_chdb_query, tenant, query)
        try:
            result = future.result(timeout=SELECT_QUERY_TIMEOUT_SECS)
            # Check if we received an error structure from execute_chdb_query
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"chDB query failed for tenant - '{tenant}': {result['error']}")
                return {
                    "status": "error",
                    "message": f"chDB query failed for tenant - '{tenant}': {result['error']}",
                }
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(
                f"chDB query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds for tenant - '{tenant}': {query}"
            )
            future.cancel()
            return {
                "status": "error",
                "message": f"chDB query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds for tenant - '{tenant}'",
            }
    except Exception as e:
        logger.error(f"Unexpected error in run_chdb_select_query for tenant - '{tenant}': {e}")
        return {"status": "error", "message": f"Unexpected error for tenant - '{tenant}': {e}"}


def chdb_initial_prompt() -> str:
    """This prompt helps users understand how to interact and perform common operations in chDB"""
    return CHDB_PROMPT


def _init_chdb_client(tenant: str):
    """Initialize the global chDB client instance."""
    if not chdb_tenant_available(tenant):
        logger.warning(f"chDB client not initialised for non-existent tenant - '{tenant}'")
        return {
            "status": "error",
            "message": f"chDB client not initialised for non-existent tenant - '{tenant}'"
        }
    
    try:
        if not get_chdb_config(tenant).enabled:
            logger.info("chDB is disabled for tenant - '{tenant}', skipping client initialization")
            return None

        client_config = get_chdb_config(tenant).get_client_config()
        data_path = client_config["data_path"]
        logger.info(f"Creating chDB client with data_path={data_path} for tenant - '{tenant}'")
        client = chs.Session(path=data_path)
        logger.info(f"Successfully connected to chDB with data_path={data_path} for tenant - '{tenant}'")
        return client
    except Exception as e:
        logger.error(f"Failed to initialize chDB client for tenant - '{tenant}': {e}")
        return None


# Register tools
if not CLICKHOUSE_TENANTS:
    logger.info("ClickHouse tools not registered")
else:
    mcp.add_tool(Tool.from_function(list_databases))
    mcp.add_tool(Tool.from_function(list_tables))
    mcp.add_tool(Tool.from_function(run_select_query))
    logger.info("ClickHouse tools registered")

if not CHDB_TENANTS:
    logger.info("chDB tools and prompts not registered")
else:
    for tenant in CHDB_TENANTS:
        _chdb_client = _init_chdb_client(tenant)
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
