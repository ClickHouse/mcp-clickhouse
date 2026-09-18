import asyncio
import concurrent.futures
import json
import logging
from typing import Optional

from mcp_clickhouse.chdb_prompt import CHDB_PROMPT
from mcp_clickhouse.executors import _Executors
from mcp_clickhouse.mcp_env import get_chdb_config, get_mcp_config
from mcp_clickhouse.serialization import _serialize_tool_result

logger = logging.getLogger("mcp-clickhouse")


def _process_chdb_result(result) -> str:
    if isinstance(result, dict) and "error" in result:
        logger.warning(f"chDB query failed: {result['error']}")
        return _serialize_tool_result({
            "status": "error",
            "message": f"chDB query failed: {result['error']}",
        })
    return _serialize_tool_result(result)


def chdb_initial_prompt() -> str:
    """This prompt helps users understand how to interact and perform common operations in chDB"""
    return CHDB_PROMPT


class _ChDBBackend:
    """chDB state and queries owned by one server assembly."""

    def __init__(self, executors: _Executors):
        self.executors = executors
        self.client = None
        self.error_message: Optional[str] = None

    def create_chdb_client(self):
        """Create a chDB client connection."""
        if not get_chdb_config().enabled:
            raise ValueError("chDB is not enabled. Set CHDB_ENABLED=true to enable it.")
        if self.client is None:
            raise RuntimeError(self.error_message or "chDB client is not available.")
        return self.client

    def execute_chdb_query(self, query: str):
        """Execute a query using chDB client."""
        client = self.create_chdb_client()
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

    def run_chdb_select_query(self, query: str) -> str:
        """Run SQL in chDB, an in-process ClickHouse engine"""
        logger.info(f"Executing chDB SELECT query: {query}")
        try:
            future = self.executors.query.submit(self.execute_chdb_query, query)
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

    async def run_chdb_select_query_async(self, query: str) -> str:
        """Async MCP-facing wrapper for chDB queries."""
        logger.info(f"Executing chDB SELECT query: {query}")
        try:
            future = self.executors.query.submit(self.execute_chdb_query, query)
            timeout_secs = get_mcp_config().query_timeout
            try:
                result = await asyncio.wait_for(
                    asyncio.wrap_future(future), timeout=timeout_secs
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"chDB query timed out after {timeout_secs} seconds: {query}"
                )
                future.cancel()
                return _serialize_tool_result({
                    "status": "error",
                    "message": f"chDB query timed out after {timeout_secs} seconds",
                })

            return await asyncio.to_thread(_process_chdb_result, result)
        except Exception as e:
            logger.error(f"Unexpected error in run_chdb_select_query_async: {e}")
            return _serialize_tool_result({"status": "error", "message": f"Unexpected error: {e}"})

    def _init_chdb_client(self):
        """Initialize the chDB client instance."""
        try:
            if not get_chdb_config().enabled:
                logger.info("chDB is disabled, skipping client initialization")
                self.error_message = None
                return None

            client_config = get_chdb_config().get_client_config()
            data_path = client_config["data_path"]
            logger.info(f"Creating chDB client with data_path={data_path}")
            import chdb.session as chs

            client = chs.Session(path=data_path)
            self.error_message = None
            logger.info(f"Successfully connected to chDB with data_path={data_path}")
            return client
        except ModuleNotFoundError as e:
            if e.name in {"chdb", "chdb.session"}:
                self.error_message = (
                    "chDB support requires the optional dependency. "
                    "Install mcp-clickhouse[chdb] to enable chDB features."
                )
                logger.warning(self.error_message)
                return None
            self.error_message = f"Failed to initialize chDB client: {e}"
            logger.error(self.error_message)
            return None
        except ImportError as e:
            self.error_message = f"Failed to initialize chDB client: {e}"
            logger.error(self.error_message)
            return None
        except Exception as e:
            self.error_message = f"Failed to initialize chDB client: {e}"
            logger.error(self.error_message)
            return None
