"""chDB-only introspection tools for mcp-clickhouse.

These tools are exposed ONLY in chDB-only mode (chDB enabled, ClickHouse server
disabled). In that mode the ClickHouse-server tools are not registered, so the
bare canonical names are free; these take them (``list_databases``,
``list_tables``, ``describe_table``, ``get_sample_data``, ``list_functions``)
and operate on the in-process chDB engine, alongside the existing
``run_chdb_select_query`` tool.

Implementation: this module is a thin adapter over ``chdb.agents.ChDBTool`` (the
canonical, cross-language chDB agent-tool contract). ChDBTool owns the shared
behavior — read-only enforcement, parameter binding, identifier quoting, result
caps, typed errors, and an optional query timeout — so mcp-clickhouse does not
re-implement any of it. ChDBTool never mutates the externally owned session it
is handed: at construction it verifies that the session's ``readonly`` setting
matches the declared ``read_only`` flag and fails with a CONFIG_MISMATCH error
otherwise (the server locks the session to ``readonly=2`` at init). The tool
signatures here keep the ClickHouse-server-parity ``(database, table)`` shape
and map onto ChDBTool's ``database=`` qualifier. See chdb.agents.CONTRACT.md.

Decoupling: self-contained; injected by the single caller
(``register_chdb_only_tools``) with a chDB-client factory, the shared executor,
and a query-timeout provider. The ClickHouse code path does not import this.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from typing import Callable, Optional

from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

logger = logging.getLogger(__name__)


def register_chdb_only_tools(
    mcp,
    *,
    max_result_bytes: int,
    create_client: Callable[[], object],
    query_executor: concurrent.futures.ThreadPoolExecutor,
    query_timeout: Callable[[], int],
    allow_write: bool = False,
) -> None:
    """Register the chDB-only introspection tools on the FastMCP instance.

    Call exactly once, only in chDB-only mode. Arguments are injected to keep
    this module decoupled from the ClickHouse server code:

    - ``max_result_bytes``: output byte cap, taken from ``ChDBConfig`` by the
      caller (not re-read from the environment here).
    - ``create_client``: returns the shared chDB client (a chdb Session).
    - ``query_executor``: thread pool to run blocking chDB work off the event loop.
    - ``query_timeout``: per-query timeout in seconds (also fed to ChDBTool's
      engine-side ``max_execution_time`` as defense in depth).
    - ``allow_write``: must match the session's mode. When False (default) the
      session must already be under ``SET readonly=2`` (the server applies it
      at init) so writes/DDL are rejected by the engine.
    """
    from chdb.agents import ChDBError, ChDBTool

    # One ChDBTool over the shared session. It probes (never mutates) the
    # session's readonly mode — construction fails with CONFIG_MISMATCH if the
    # flag disagrees with the session — and applies the byte cap and the
    # engine-side timeout.
    tool = ChDBTool(
        session=create_client(),
        read_only=not allow_write,
        max_bytes=max_result_bytes,
        max_execution_time=query_timeout(),
    )

    async def _run(work: Callable[[], object], tool_name: str) -> str:
        """Run a ChDBTool call on the executor with a timeout; return JSON text.

        Output mirrors the ClickHouse-server tools: success is bare JSON, and any
        failure (engine error or timeout) is raised as a ``ToolError`` — the same
        way ``run_query`` surfaces errors — rather than returned as a string."""
        timeout = query_timeout()
        future = query_executor.submit(work)
        try:
            result = await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
        except asyncio.TimeoutError:
            future.cancel()
            logger.warning("chDB %s timed out after %ss", tool_name, timeout)
            raise ToolError(f"chDB {tool_name} timed out after {timeout} seconds")
        except ChDBError as err:
            raise ToolError(err.message)
        except Exception as err:  # noqa: BLE001
            logger.error("chDB %s failed: %s", tool_name, err)
            raise ToolError(str(err))
        return json.dumps(result, ensure_ascii=False, default=str)

    async def list_databases() -> str:
        """List databases in the in-process chDB engine."""
        return await _run(tool.list_databases, "list_databases")

    async def list_tables(database: str) -> str:
        """List tables in a chDB database.

        Args:
            database: Database name (plain SQL identifier).
        """
        return await _run(lambda: tool.list_tables(database), "list_tables")

    async def describe_table(database: str, table: str) -> str:
        """Return column names and types for a chDB table.

        Args:
            database: Database name (plain identifier).
            table: Table name (plain identifier).
        """
        return await _run(lambda: tool.describe(table, database=database), "describe_table")

    async def get_sample_data(database: str, table: str, limit: int = 10) -> str:
        """Return the first rows of a chDB table.

        Args:
            database: Database name (plain identifier).
            table: Table name (plain identifier).
            limit: Maximum rows to return; clamped to [1, 1000].
        """
        n = max(1, min(int(limit), 1000))
        return await _run(
            lambda: tool.get_sample_data(table, database=database, limit=n).rows,
            "get_sample_data",
        )

    async def list_functions(pattern: Optional[str] = None) -> str:
        """List SQL functions available in the chDB engine.

        Args:
            pattern: Optional case-insensitive substring filter on the function
                name.
        """
        # `pattern` is a substring; ChDBTool's `like` is a raw LIKE pattern, so
        # wrap with %...% to preserve the substring-match semantics.
        like = f"%{pattern}%" if pattern else None
        return await _run(lambda: tool.list_functions(like=like), "list_functions")

    tools = (
        (list_databases, "list_databases", "List databases in the in-process chDB engine."),
        (list_tables, "list_tables", "List tables in a chDB database."),
        (describe_table, "describe_table", "Return column names and types for a chDB table."),
        (get_sample_data, "get_sample_data", "Return the first rows of a chDB table."),
        (list_functions, "list_functions", "List SQL functions available in the chDB engine."),
    )
    for fn, name, description in tools:
        mcp.add_tool(Tool.from_function(fn, name=name, description=description))
    logger.info("chDB-only introspection tools registered (%d tools, over chdb.agents.ChDBTool)", len(tools))
