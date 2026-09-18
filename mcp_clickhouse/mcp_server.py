import atexit
import logging
import os
from importlib.metadata import PackageNotFoundError, version as package_version

from fastmcp.prompts import Prompt
from fastmcp.tools import Tool

from mcp_clickhouse import clients
from mcp_clickhouse.auth import _initialize_auth_parser, _load_default_dotenv
from mcp_clickhouse.chdb_backend import _ChDBBackend, chdb_initial_prompt as chdb_initial_prompt
from mcp_clickhouse.clients import CLIENT_CONFIG_OVERRIDES_KEY as CLIENT_CONFIG_OVERRIDES_KEY
from mcp_clickhouse.executors import _Executors
from mcp_clickhouse.health import _Health
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

_clickhouse_clients = clients._ClickHouseClients()

_queries = _Queries(_executors, _clickhouse_clients)

_health = _Health(_executors, _clickhouse_clients)

_initialize_auth_parser()

mcp = ClickHouseFastMCP(
    name=MCP_SERVER_NAME,
    version=MCP_SERVER_VERSION,
    instructions=CLICKHOUSE_SERVER_INSTRUCTIONS,
)
_chdb_backend = _ChDBBackend(_executors)


_health.chdb_backend = _chdb_backend
health_check = mcp.custom_route("/health", methods=["GET"])(_health.health_check)


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
