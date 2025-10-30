"""chDB MCP service implementation."""

import logging
import json
import concurrent.futures
import atexit

import chdb.session as chs
from fastmcp import FastMCP
from fastmcp.tools import Tool
from fastmcp.prompts import Prompt

from ..config import get_chdb_config, get_mcp_config
from .prompts import CHDB_PROMPT

logger = logging.getLogger("mcp-clickhouse.chdb")

# Query executor
QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)
atexit.register(lambda: QUERY_EXECUTOR.shutdown(wait=True))

# Global chDB client
_chdb_client = None


def _init_chdb_client():
    """Initialize global chDB client instance."""
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


def create_chdb_client():
    """Create chDB client connection."""
    global _chdb_client
    if not get_chdb_config().enabled:
        raise ValueError("chDB is not enabled. Set CHDB_ENABLED=true to enable it.")
    
    # Initialize client if not already initialized
    if _chdb_client is None:
        _chdb_client = _init_chdb_client()
    
    return _chdb_client


def execute_chdb_query(query: str):
    """Execute query using chDB client."""
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
    """Run SQL in chDB, an in-process ClickHouse engine.
    
    chDB = In-Process ClickHouse + Direct Query via Table Functions
    
    Key Features:
    1. Table Functions - Query data sources directly without import:
       - Local files: file('path/to/file.csv') or file('data.parquet', 'Parquet')
       - Remote files: url('https://example.com/data.csv', 'CSV')
       - S3 storage: s3('s3://bucket/path/file.csv', 'CSV')
       - PostgreSQL: postgresql('host:port', 'database', 'table', 'user', 'password')
       - MySQL: mysql('host:port', 'database', 'table', 'user', 'password')
    
    2. Supported Formats:
       - CSV, TSV, JSON, JSONEachRow
       - Parquet, ORC, Avro
    
    3. Best Practices:
       - Use LIMIT to prevent large result sets (recommend LIMIT 10 by default)
       - Use WHERE to filter and reduce data transfer
       - Use SELECT to specify columns and avoid full table scans
       - Multi-source JOIN: file() JOIN url() JOIN s3()
       - Test connection: DESCRIBE table_function(...)
    
    4. No Data Import Required:
       - Query data in place
       - If no suitable table function exists, use Python to download to temp file and query with file()
    """
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
            logger.warning(
                f"chDB query timed out after {timeout_secs} seconds: {query}"
            )
            future.cancel()
            return {
                "status": "error",
                "message": f"chDB query timed out after {timeout_secs} seconds",
            }
    except Exception as e:
        logger.error(f"Unexpected error in run_chdb_select_query: {e}")
        return {"status": "error", "message": f"Unexpected error: {e}"}


def chdb_initial_prompt() -> str:
    """This prompt helps users understand how to interact with chDB and perform common operations"""
    return CHDB_PROMPT


def register_tools(mcp: FastMCP):
    """Register chDB tools to MCP instance."""
    global _chdb_client
    
    _chdb_client = _init_chdb_client()
    if _chdb_client:
        atexit.register(lambda: _chdb_client.close())

    mcp.add_tool(Tool.from_function(run_chdb_select_query))
    chdb_prompt = Prompt.from_function(
        chdb_initial_prompt,
        name="chdb_initial_prompt",
        description="This prompt helps users understand how to interact with chDB and perform common operations",
    )
    mcp.add_prompt(chdb_prompt)
    logger.info("chDB tools and prompts registered")

