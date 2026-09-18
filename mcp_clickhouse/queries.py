import asyncio
import concurrent.futures
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from clickhouse_connect.driver.binding import external_bind_re, format_query_value
from fastmcp.exceptions import ToolError

from mcp_clickhouse import clients
from mcp_clickhouse.executors import _Executors
from mcp_clickhouse.mcp_env import get_config, get_mcp_config
from mcp_clickhouse.serialization import _serialize_tool_result

logger = logging.getLogger("mcp-clickhouse")

_QUERY_CANCELLATION_WAIT_SECONDS = 1.0


@dataclass
class _ActiveQueryState:
    query: str
    client_entry: Optional[clients._ClientCacheEntry] = None
    cancelled: bool = False


# SQL comments and quoted text, blanked out before destructive-keyword matching.
# A keyword inside a string literal must not trigger the guard, and a keyword
# placed after a comment marker must not slip past it.
_SQL_COMMENTS_AND_QUOTED_TEXT = re.compile(
    r"""
      '(?:\\.|''|[^'\\])*'              # string literal
    | "(?:\\.|""|[^"\\])*"              # double-quoted identifier
    | `(?:\\.|``|[^`\\])*`              # backtick-quoted identifier
    | \$(?P<tag>\w*)\$.*?\$(?P=tag)\$    # dollar-quoted string or heredoc
    | --[^\n]*                         # line comment
    | \#[^\n]*                         # line comment
    | /\*.*?\*/                        # block comment
    """,
    re.VERBOSE | re.DOTALL,
)

# Matched against the scrubbed statement. Bare DROP also covers the
# ALTER ... DROP PARTITION/PART/COLUMN clauses. TRUNCATE followed by an open
# parenthesis is the rounding function, as is replace() after OR. Bare DELETE
# and UPDATE cover both the lightweight and ALTER mutation forms. REPLACE
# TABLE/PARTITION and OR REPLACE overwrite existing data. Bare CLEAR is
# reversible, so only CLEAR COLUMN/INDEX/PROJECTION is flagged.
_DESTRUCTIVE_KEYWORDS = re.compile(
    r"""
      \bDROP\b
    | \bTRUNCATE\b(?!\s*\()
    | \bDELETE\b
    | \bUPDATE\b
    | \bREPLACE\s+(?:TABLE|PARTITION)\b
    | \bOR\s+REPLACE\b(?!\s*\()
    | \bCLEAR\s+(?:COLUMN|INDEX|PROJECTION)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# DETACH ... PERMANENTLY is matched as two independent searches. A single
# `DETACH .* PERMANENTLY` branch backtracks quadratically on crafted input,
# and the validator runs on an executor thread that cancel cannot stop.
# Plain DETACH is reversible via ATTACH and stays allowed.
_DETACH_KEYWORD = re.compile(r"\bDETACH\b", re.IGNORECASE)
_PERMANENTLY_KEYWORD = re.compile(r"\bPERMANENTLY\b", re.IGNORECASE)
# Only mask the parameter name. Types and surrounding SQL remain visible.
_SQL_PARAMETER_NAME = re.compile(r"\{\s*[A-Za-z_$][A-Za-z0-9_$]*\s*:")
_SQL_PARAMETER_PREFIX = re.compile(r"\{[\w$]+:")


def _has_unclosed_placeholder(query: str, max_scan: int = 1_000_000) -> bool:
    """True if unterminated {name: placeholder starts would backtrack too far.

    external_bind_re's [^}]+ scans to the end of the string for every driver-valid
    {name: that has no closing } after it, which is only possible after the last }.
    A few such starts (placeholder-like text in a trailing comment or literal) are one
    linear scan each and harmless; a flood is quadratic here and in the driver's own
    bind path. Bounding the total scanned length keeps both linear. Only meaningful
    when params are supplied.
    """
    length = len(query)
    scanned = 0
    for prefix in _SQL_PARAMETER_PREFIX.finditer(query, query.rfind("}") + 1):
        if external_bind_re.fullmatch(prefix.group() + "String}") is not None:
            scanned += length - prefix.start()
            if scanned > max_scan:
                return True
    return False


def _strip_comments_and_quoted_text(query: str) -> str:
    """Blank out comments and quoted text so keyword matching sees only SQL syntax.

    Each match is replaced by a single space to keep surrounding tokens separate.
    Unterminated literals and comments do not match and are left in place, which
    keeps the destructive-operation check on the conservative side.
    """
    return _SQL_COMMENTS_AND_QUOTED_TEXT.sub(" ", query)


def _validate_query_for_destructive_ops(query: str) -> None:
    """Reject destructive statements unless CLICKHOUSE_ALLOW_DROP is set.

    Args:
        query: The SQL query to validate

    Raises:
        ToolError: If the query contains a destructive statement and CLICKHOUSE_ALLOW_DROP is not set
    """
    config = get_config()

    # If writes are not enabled, skip this check (readonly mode will catch it anyway)
    if not config.allow_write_access:
        return

    # If DROP is explicitly allowed, no validation needed
    if config.allow_drop:
        return

    statement = _strip_comments_and_quoted_text(query)
    statement = _SQL_PARAMETER_NAME.sub(" ", statement)
    if _DESTRUCTIVE_KEYWORDS.search(statement) or (
        _DETACH_KEYWORD.search(statement) and _PERMANENTLY_KEYWORD.search(statement)
    ):
        raise ToolError(
            "Destructive operations are not allowed (DROP, TRUNCATE, DELETE, UPDATE, "
            "REPLACE TABLE/PARTITION, CREATE OR REPLACE, CLEAR COLUMN/INDEX/PROJECTION, "
            "DETACH PERMANENTLY). Set CLICKHOUSE_ALLOW_DROP=true to enable them. "
            "This gate is a best-effort accident guard, not a security boundary. "
            "Restrict the ClickHouse user's grants for real enforcement."
        )


class _Queries:
    """ClickHouse query execution and cancellation owned by one server assembly."""

    def __init__(self, executors: _Executors, clickhouse_clients: clients._ClickHouseClients):
        self.executors = executors
        self.clients = clickhouse_clients
        self.active_queries: Dict[str, _ActiveQueryState] = {}
        self.active_queries_lock = threading.Lock()

    def _register_active_query(self, query_id: str, query: str) -> _ActiveQueryState:
        """Register query state before its worker is submitted."""
        state = _ActiveQueryState(query=query)
        with self.active_queries_lock:
            self.active_queries[query_id] = state
        return state

    def _remove_active_query(self, query_id: str, state: _ActiveQueryState) -> None:
        """Remove query state if it still belongs to this execution."""
        with self.active_queries_lock:
            if self.active_queries.get(query_id) is state:
                self.active_queries.pop(query_id)

    def _mark_active_query_cancelled(self, query_id: str) -> Optional[_ActiveQueryState]:
        """Mark an active query cancelled before any server-side KILL attempt."""
        with self.active_queries_lock:
            state = self.active_queries.get(query_id)
            if state is not None:
                state.cancelled = True
            return state

    def execute_query(
        self,
        query: str,
        query_id: str,
        client_config: dict,
        params: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Execute a query in a worker thread with a pre-resolved client config."""
        with self.active_queries_lock:
            state = self.active_queries.get(query_id)
            if state is None:
                state = _ActiveQueryState(query=query)
                self.active_queries[query_id] = state

        entry = None
        try:
            if params is not None and (
                not isinstance(params, dict) or any(not isinstance(key, str) for key in params)
            ):
                raise ToolError("params must be an object with string keys")
            if params and _has_unclosed_placeholder(query):
                raise ToolError(
                    "Too many unterminated ClickHouse {name:Type} placeholder starts. "
                    "Close the braces. Placeholder-like text in a comment or string "
                    "literal counts toward this when params is set."
                )
            # Keep parameters out of the driver's client-side formatting and raw binary paths.
            if params and (
                external_bind_re.search(query) is None
                or any(len(key) > 1 and key.startswith("$") and key.endswith("$") for key in params)
            ):
                raise ToolError(
                    "params requires ClickHouse {name:Type} placeholders with the opening brace, "
                    "name, and colon adjacent, for example {id:UInt32}. Spaces within the type "
                    "are allowed. Python percent "
                    "formatting and $name$ raw binary parameters are not supported."
                )
            entry = self.clients._acquire_clickhouse_client(client_config)
            client = entry.client
            with self.active_queries_lock:
                if state.cancelled:
                    raise ToolError("Query cancelled before execution")
                state.client_entry = entry

            _validate_query_for_destructive_ops(query)

            query_settings = clients.build_query_settings(client)
            query_settings["query_id"] = query_id
            with self.active_queries_lock:
                if state.cancelled:
                    raise ToolError("Query cancelled before execution")
            res = client.query(query, parameters=params, settings=query_settings)
            logger.info(f"Query {query_id} returned {len(res.result_rows)} rows")
            return _serialize_tool_result({"columns": res.column_names, "rows": res.result_rows})
        except ToolError:
            raise
        except Exception as err:
            # Do not retry queries because a write may already have succeeded.
            if entry is not None and clients._is_connection_error(err):
                self.clients._evict_cached_client(client_config, client)
            logger.error(f"Error executing query {query_id}: {err}")
            raise ToolError(f"Query execution failed: {str(err)}")
        finally:
            self._remove_active_query(query_id, state)
            if entry is not None:
                self.clients._release_client_entry(entry)

    def _cancel_query(self, query_id: str):
        """Issue KILL QUERY on the ClickHouse server for a timed-out query.

        Uses the same cached client that originated the query. Cancellation
        failures are logged without masking the original timeout.
        """
        state = self._mark_active_query_cancelled(query_id)

        if state is None:
            logger.debug("Query %s already completed, nothing to cancel", query_id)
            return

        try:
            safe_id = str(uuid.UUID(query_id))
        except ValueError:
            logger.warning("Refusing to KILL QUERY with non-UUID query_id: %r", query_id)
            return

        client = None
        try:
            client_entry = state.client_entry
            client = self.clients._retain_client_entry(client_entry)
            if client is None:
                logger.warning(
                    "Query %s cancelled before client acquisition completed",
                    safe_id,
                )
                return

            logger.info("Cancelling query %s via KILL QUERY", safe_id)
            client.command(
                f"KILL QUERY WHERE query_id = {format_query_value(safe_id)}"
            )
            logger.info("Successfully cancelled query %s", safe_id)
        except Exception as e:
            logger.warning("Failed to cancel query %s: %s", safe_id, e)
        finally:
            if client is not None:
                self.clients._release_client_entry(client_entry)

    def _cancel_query_with_bounded_wait(self, query_id: str) -> None:
        """Run cancellation in its executor and wait briefly for completion."""
        future = self.executors.cancellation.submit(self._cancel_query, query_id)
        try:
            future.result(timeout=_QUERY_CANCELLATION_WAIT_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "Cancellation for query %s exceeded %.1f seconds",
                query_id,
                _QUERY_CANCELLATION_WAIT_SECONDS,
            )

    async def _cancel_query_async(self, query_id: str) -> None:
        """Await cancellation briefly without blocking the event loop."""
        future = self.executors.cancellation.submit(self._cancel_query, query_id)
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)),
                timeout=_QUERY_CANCELLATION_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Cancellation for query %s exceeded %.1f seconds",
                query_id,
                _QUERY_CANCELLATION_WAIT_SECONDS,
            )

    def run_query(self, query: str, params: Optional[Dict[str, Any]] = None) -> str:
        """Execute a SQL query against ClickHouse.

        Queries run in read-only mode by default. Set CLICKHOUSE_ALLOW_WRITE_ACCESS=true
        to allow DDL and DML statements when your ClickHouse server permits them.

        Bind params by name with ClickHouse placeholders such as {name:String} or
        {vector:Array(Float32)}. Values may be JSON scalars, nulls, or arrays. Pass exact
        large integers as decimal strings. JSON lists and objects cannot bind to Tuple
        and Map types. Python percent formatting and $name$ raw binary parameters are
        not supported. Parameter values stay out of the MCP server's normal SQL log lines,
        but may appear in errors and backend logs.
        """
        logger.info(f"Executing query: {query}")

        client_config = clients._resolve_client_config()
        query_id = str(uuid.uuid4())
        state = self._register_active_query(query_id, query)

        try:
            with self.active_queries_lock:
                in_flight = len(self.active_queries)
            if in_flight >= self.executors.max_workers:
                logger.warning(
                    "Thread pool saturated: %d in-flight vs %d workers",
                    in_flight, self.executors.max_workers,
                )

            try:
                future = self.executors.query.submit(self.execute_query, query, query_id, client_config, params)
            except Exception:
                self._remove_active_query(query_id, state)
                raise
            timeout_secs = get_mcp_config().query_timeout
            try:
                return future.result(timeout=timeout_secs)
            except concurrent.futures.TimeoutError:
                logger.warning(
                    "Query %s timed out after %s seconds: %s", query_id, timeout_secs, query
                )
                if future.cancel():
                    self._remove_active_query(query_id, state)
                else:
                    self._mark_active_query_cancelled(query_id)
                    self._cancel_query_with_bounded_wait(query_id)
                raise ToolError(f"Query timed out after {timeout_secs} seconds")
        except ToolError:
            raise
        except Exception as e:
            logger.error("Unexpected error in run_query: %s", str(e))
            raise RuntimeError(f"Unexpected error during query execution: {str(e)}")

    async def run_query_async(self, query: str, params: Optional[Dict[str, Any]] = None) -> str:
        """Async MCP-facing wrapper for ClickHouse queries.

        Awaits the worker-pool future asynchronously so concurrent tool calls are
        served while a slow query is in flight. Bind params by name with {name:Type}
        placeholders. Values may be JSON scalars, nulls, or arrays. JSON lists and
        objects cannot bind to Tuple and Map types. Python percent
        formatting and $name$ raw binary parameters are not supported.
        """
        logger.info(f"Executing query: {query}")

        overrides = await clients._get_client_config_overrides_for_tool()
        client_config = clients._resolve_client_config(overrides)
        query_id = str(uuid.uuid4())
        state = self._register_active_query(query_id, query)

        try:
            with self.active_queries_lock:
                in_flight = len(self.active_queries)
            if in_flight >= self.executors.max_workers:
                logger.warning(
                    "Thread pool saturated: %d in-flight vs %d workers",
                    in_flight, self.executors.max_workers,
                )

            try:
                future = self.executors.query.submit(self.execute_query, query, query_id, client_config, params)
            except Exception:
                self._remove_active_query(query_id, state)
                raise
            timeout_secs = get_mcp_config().query_timeout
            try:
                return await asyncio.wait_for(
                    asyncio.wrap_future(future), timeout=timeout_secs
                )
            except asyncio.CancelledError:
                if future.cancel():
                    self._remove_active_query(query_id, state)
                else:
                    self._mark_active_query_cancelled(query_id)
                    await self._cancel_query_async(query_id)
                raise
            except asyncio.TimeoutError:
                logger.warning(
                    "Query %s timed out after %s seconds: %s", query_id, timeout_secs, query
                )
                if future.cancel():
                    self._remove_active_query(query_id, state)
                else:
                    self._mark_active_query_cancelled(query_id)
                    await self._cancel_query_async(query_id)
                raise ToolError(f"Query timed out after {timeout_secs} seconds")
        except ToolError:
            raise
        except Exception as e:
            logger.error("Unexpected error in run_query_async: %s", str(e))
            raise RuntimeError(f"Unexpected error during query execution: {str(e)}")
