"""Optional Postgres backend: connections, guarded query execution, and catalog discovery."""

import asyncio
import base64
import binascii
import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Annotated, Any, Optional

from fastmcp.exceptions import ToolError
from pydantic import Field

from mcp_clickhouse.executors import _Executors
from mcp_clickhouse.mcp_env import get_mcp_config, get_postgres_config
from mcp_clickhouse.serialization import _serialize_tool_result

logger = logging.getLogger("mcp-clickhouse")

_CANCEL_TIMEOUT_SECONDS = 1.0
_CANCEL_WAIT_SECONDS = 1.0

# Comments and quoted text in Postgres syntax, blanked out before keyword matching.
# Unlike ClickHouse, # is an operator rather than a comment and backticks carry no
# meaning. Backslash escapes only apply in E'' strings. Dollar-quoted text is left in
# place so DO blocks and function bodies are still checked.
_POSTGRES_COMMENTS_AND_QUOTED_TEXT = re.compile(
    r"""
      \b[Ee]'(?:\\.|''|[^'\\])*'      # escape string
    | '(?:''|[^'])*'                  # standard string
    | "(?:""|[^"])*"                  # quoted identifier
    | --[^\n]*                        # line comment
    | /\*.*?\*/                       # block comment
    """,
    re.VERBOSE | re.DOTALL,
)

# Statements that end the tool's transaction or start another. PREPARE TRANSACTION
# would leave a prepared transaction behind that pins the vacuum horizon.
_POSTGRES_TRANSACTION_CONTROL = re.compile(
    r"\s*(?:COMMIT|END|ROLLBACK|ABORT|BEGIN|START\s+TRANSACTION|PREPARE\s+TRANSACTION)\b",
    re.IGNORECASE,
)

# Clauses that name DELETE or UPDATE without deleting or updating anything:
# foreign key actions, trigger events, and privilege lists.
_POSTGRES_NON_DESTRUCTIVE_CLAUSES = re.compile(
    r"""
      \bON\s+(?:DELETE|UPDATE)\b
    | \b(?:BEFORE|AFTER|OF|OR)\s+(?:DELETE|UPDATE|TRUNCATE)\b
    | ^\s*(?:GRANT|REVOKE)\b.*
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

# Bare DROP also covers ALTER ... DROP COLUMN/CONSTRAINT. UPDATE also matches
# ON CONFLICT DO UPDATE, MERGE ... THEN UPDATE, and SELECT ... FOR UPDATE.
# OR REPLACE overwrites an existing view, function, or rule.
_POSTGRES_DESTRUCTIVE_KEYWORDS = re.compile(
    r"\bDROP\b|\bTRUNCATE\b|\bDELETE\b|\bUPDATE\b|\bOR\s+REPLACE\b",
    re.IGNORECASE,
)


def _validate_postgres_query_for_transaction_control(query: str) -> None:
    """Reject statements that would end or replace the tool's own transaction."""
    statement = _POSTGRES_COMMENTS_AND_QUOTED_TEXT.sub(" ", query)
    if _POSTGRES_TRANSACTION_CONTROL.match(statement):
        raise ToolError(
            "Transaction control statements (BEGIN, COMMIT, ROLLBACK, PREPARE TRANSACTION) "
            "are not allowed. Each call already runs in its own transaction."
        )

def _validate_postgres_query_for_destructive_ops(query: str) -> None:
    """Reject destructive statements unless POSTGRES_ALLOW_DROP is set.

    Read-only mode needs no check because Postgres rejects writes in a READ ONLY
    transaction.
    """
    config = get_postgres_config()
    if not config.allow_write_access or config.allow_drop:
        return

    statement = _POSTGRES_COMMENTS_AND_QUOTED_TEXT.sub(" ", query)
    statement = _POSTGRES_NON_DESTRUCTIVE_CLAUSES.sub(" ", statement)
    if _POSTGRES_DESTRUCTIVE_KEYWORDS.search(statement):
        raise ToolError(
            "Destructive operations are not allowed (DROP, TRUNCATE, DELETE, UPDATE, "
            "CREATE OR REPLACE). Set POSTGRES_ALLOW_DROP=true to enable them. "
            "This gate is a best-effort accident guard, not a security boundary. "
            "Restrict the Postgres role's privileges for real enforcement."
        )


def _encode_page_token(state: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(state).encode()).decode()


def _decode_page_token(token: str) -> dict[str, Any]:
    try:
        state = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (binascii.Error, ValueError, UnicodeError):
        raise ToolError("Invalid page_token")
    if not isinstance(state, dict) or not isinstance(state.get("after"), str):
        raise ToolError("Invalid page_token")
    return state


@dataclass
class _PostgresQueryState:
    connection: Any = None
    cancelled: bool = False


class _PostgresBackend:
    """Postgres connections and queries owned by one server assembly.

    Each call opens its own connection and runs one statement in its own
    transaction, so session settings cannot leak between MCP sessions.
    """

    def __init__(self, executors: _Executors):
        self.executors = executors
        self.psycopg = None
        self.error_message: Optional[str] = None
        self._state_lock = threading.Lock()

    def _init_driver(self) -> bool:
        """Import psycopg and report whether the Postgres tools can be registered."""
        self.psycopg = None
        try:
            import psycopg

            if not psycopg.Pipeline.is_supported():
                self.error_message = (
                    "Postgres support requires libpq 14 or later for pipeline mode. "
                    "Install mcp-clickhouse[postgres] to use the bundled libpq."
                )
                logger.error(self.error_message)
                return False
            self.psycopg = psycopg
            self.error_message = None
            return True
        except ModuleNotFoundError as e:
            if e.name == "psycopg":
                self.error_message = (
                    "Postgres support requires the optional dependency. "
                    "Install mcp-clickhouse[postgres] to enable Postgres features."
                )
                logger.warning(self.error_message)
                return False
            self.error_message = f"Failed to initialize the Postgres driver: {e}"
            logger.error(self.error_message)
            return False
        except ImportError as e:
            self.error_message = f"Failed to initialize the Postgres driver: {e}"
            logger.error(self.error_message)
            return False

    def _connect(self, read_only: bool):
        if self.psycopg is None:
            raise ToolError(self.error_message or "Postgres driver is not available.")
        try:
            conn = self.psycopg.connect(**get_postgres_config().get_connect_kwargs())
        except self.psycopg.Error as err:
            logger.error("Postgres connection failed: %s", err)
            raise ToolError(f"Postgres connection failed: {err}")
        # None keeps the server default, so write mode still works against a standby
        # for reads instead of failing on BEGIN READ WRITE.
        conn.read_only = True if read_only else None
        return conn

    def _begin(self, cursor) -> None:
        """Start the transaction and bound its statements by the tool timeout."""
        timeout_ms = get_mcp_config().query_timeout * 1000
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", (f"{timeout_ms}ms",))

    def execute_query(self, query: str, state: Optional[_PostgresQueryState] = None) -> str:
        """Run one statement in a worker thread and return a JSON-encoded result."""
        if state is None:
            state = _PostgresQueryState()
        _validate_postgres_query_for_transaction_control(query)
        _validate_postgres_query_for_destructive_ops(query)
        allow_write = get_postgres_config().allow_write_access

        conn = self._connect(read_only=not allow_write)
        try:
            with self._state_lock:
                if state.cancelled:
                    raise ToolError("Query cancelled before execution")
                state.connection = conn
            with conn.cursor() as cursor:
                # Pipeline mode always uses the extended query protocol, which rejects
                # a string holding several statements. That keeps COMMIT; ... from
                # escaping the READ ONLY transaction.
                with conn.pipeline():
                    self._begin(cursor)
                    cursor.execute(query)
                if cursor.description is None:
                    columns, rows = [], []
                else:
                    columns = [column.name for column in cursor.description]
                    rows = [list(row) for row in cursor.fetchall()]
            if conn.info.transaction_status != self.psycopg.pq.TransactionStatus.INTRANS:
                raise ToolError("Statement ended the tool's transaction")
            with self._state_lock:
                # The caller has already reported a timeout or cancellation, so a
                # late write must not be committed.
                commit = allow_write and not state.cancelled
                state.connection = None
            if commit:
                conn.commit()
            else:
                conn.rollback()
            if state.cancelled:
                raise ToolError("Query cancelled; the transaction was rolled back")
            logger.info("Postgres query returned %s rows", len(rows))
            return _serialize_tool_result({"columns": columns, "rows": rows})
        except ToolError:
            raise
        except self.psycopg.Error as err:
            logger.error("Error executing Postgres query: %s", err)
            raise ToolError(f"Query execution failed: {err}")
        finally:
            with self._state_lock:
                state.connection = None
            conn.close()

    def _cancel(self, state: _PostgresQueryState) -> None:
        """Ask the server to cancel the statement running for state, if any."""
        with self._state_lock:
            state.cancelled = True
            conn = state.connection
        if conn is None:
            return
        try:
            conn.cancel_safe(timeout=_CANCEL_TIMEOUT_SECONDS)
            logger.info("Cancelled Postgres query")
        except Exception as e:
            logger.warning("Failed to cancel Postgres query: %s", e)

    async def run_postgres_query_async(self, query: str) -> str:
        """Async MCP-facing wrapper for Postgres queries."""
        logger.info("Executing Postgres query: %s", query)
        state = _PostgresQueryState()
        future = self.executors.query.submit(self.execute_query, query, state)
        timeout_secs = get_mcp_config().query_timeout
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout_secs)
        except asyncio.CancelledError:
            if not future.cancel():
                await self._cancel_with_bounded_wait(state)
            raise
        except asyncio.TimeoutError:
            logger.warning("Postgres query timed out after %s seconds: %s", timeout_secs, query)
            if not future.cancel():
                await self._cancel_with_bounded_wait(state)
            raise ToolError(f"Query timed out after {timeout_secs} seconds")

    async def _cancel_with_bounded_wait(self, state: _PostgresQueryState) -> None:
        """Run cancellation in its executor and wait briefly for it."""
        future = self.executors.cancellation.submit(self._cancel, state)
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)), timeout=_CANCEL_WAIT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Postgres cancellation exceeded %.1f seconds", _CANCEL_WAIT_SECONDS
            )

    def _fetch_catalog(self, query: str, params: Optional[dict] = None) -> list[dict]:
        """Run a catalog query in a read-only transaction and return rows as dicts."""
        conn = self._connect(read_only=True)
        try:
            with conn.cursor() as cursor:
                self._begin(cursor)
                cursor.execute(query, params)
                columns = [column.name for column in cursor.description]
                rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            conn.rollback()
            return rows
        except self.psycopg.Error as err:
            logger.error("Error reading Postgres catalog: %s", err)
            raise ToolError(f"Postgres catalog query failed: {err}")
        finally:
            conn.close()

    def list_schemas(self) -> str:
        """List Postgres schemas the configured role can use."""
        rows = self._fetch_catalog(
            """
            SELECT nspname AS name
            FROM pg_catalog.pg_namespace
            WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND nspname NOT LIKE 'pg\\_temp\\_%'
              AND nspname NOT LIKE 'pg\\_toast\\_temp\\_%'
              AND has_schema_privilege(oid, 'USAGE')
            ORDER BY nspname
            """
        )
        return _serialize_tool_result([row["name"] for row in rows])

    def list_tables(
        self,
        schema: str,
        like: Optional[str],
        not_like: Optional[str],
        page_token: Optional[str],
        page_size: int,
        include_detailed_columns: bool,
    ) -> str:
        """List one page of relations in a schema."""
        if page_size <= 0:
            raise ToolError("page_size must be greater than 0")
        after = None
        if page_token:
            token_state = _decode_page_token(page_token)
            if (token_state.get("schema"), token_state.get("like"), token_state.get("not_like")) != (
                schema,
                like,
                not_like,
            ):
                raise ToolError("page_token was issued for a different schema or filters")
            after = token_state["after"]

        filters = """
            n.nspname = %(schema)s
            AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
            AND NOT c.relispartition
            AND (%(like)s::text IS NULL OR c.relname LIKE %(like)s)
            AND (%(not_like)s::text IS NULL OR c.relname NOT LIKE %(not_like)s)
        """
        params = {
            "schema": schema,
            "like": like,
            "not_like": not_like,
            "after": after,
            "limit": page_size + 1,
        }
        tables = self._fetch_catalog(
            f"""
            SELECT
                n.nspname AS schema,
                c.relname AS name,
                CASE c.relkind
                    WHEN 'r' THEN 'table'
                    WHEN 'p' THEN 'partitioned table'
                    WHEN 'v' THEN 'view'
                    WHEN 'm' THEN 'materialized view'
                    WHEN 'f' THEN 'foreign table'
                END AS kind,
                obj_description(c.oid, 'pg_class') AS comment,
                CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END
                    AS estimated_rows,
                pg_total_relation_size(c.oid) AS total_bytes,
                (
                    SELECT array_agg(a.attname ORDER BY k.ord)
                    FROM pg_index i
                    CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
                    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                    WHERE i.indrelid = c.oid AND i.indisprimary
                ) AS primary_key
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE {filters}
              AND (%(after)s::name IS NULL OR c.relname > %(after)s::name)
            ORDER BY c.relname
            LIMIT %(limit)s
            """,
            params,
        )
        total_tables = self._fetch_catalog(
            f"""
            SELECT count(*) AS total
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE {filters}
            """,
            params,
        )[0]["total"]

        has_more = len(tables) > page_size
        tables = tables[:page_size]
        for table in tables:
            table["columns"] = []

        if include_detailed_columns and tables:
            columns = self._fetch_catalog(
                """
                SELECT
                    c.relname AS table,
                    a.attname AS name,
                    format_type(a.atttypid, a.atttypmod) AS column_type,
                    NOT a.attnotnull AS nullable,
                    CASE
                        WHEN a.attgenerated <> '' THEN 'generated'
                        WHEN a.attidentity <> '' THEN 'identity'
                        WHEN d.adbin IS NOT NULL THEN 'default'
                    END AS default_kind,
                    pg_get_expr(d.adbin, d.adrelid) AS default_expression,
                    col_description(c.oid, a.attnum) AS comment
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                WHERE n.nspname = %(schema)s
                  AND c.relname = ANY(%(names)s)
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                ORDER BY c.relname, a.attnum
                """,
                {"schema": schema, "names": [table["name"] for table in tables]},
            )
            by_name = {table["name"]: table for table in tables}
            for column in columns:
                by_name[column.pop("table")]["columns"].append(column)

        next_page_token = None
        if has_more:
            next_page_token = _encode_page_token({
                "schema": schema,
                "like": like,
                "not_like": not_like,
                "after": tables[-1]["name"],
            })
        return _serialize_tool_result({
            "tables": tables,
            "next_page_token": next_page_token,
            "total_tables": total_tables,
        })

    async def list_postgres_schemas_async(self) -> str:
        """List Postgres schemas the configured role has USAGE on, excluding system schemas."""
        future = self.executors.metadata.submit(self.list_schemas)
        return await asyncio.wrap_future(future)

    async def list_postgres_tables_async(
        self,
        schema: str,
        like: Optional[str] = None,
        not_like: Optional[str] = None,
        page_token: Optional[str] = None,
        page_size: Annotated[int, Field(gt=0)] = 50,
        include_detailed_columns: bool = True,
    ) -> str:
        """List tables, views, materialized views, and foreign tables in a Postgres schema.

        Partitions are omitted; their partitioned parent is listed. estimated_rows comes
        from planner statistics and is null when the relation has never been analyzed.
        Integers outside [-9007199254740991, 9007199254740991] are returned as decimal
        strings.

        Args:
            schema: The schema to list tables from
            like: Optional LIKE pattern to filter table names
            not_like: Optional NOT LIKE pattern to exclude table names
            page_token: Token from a previous call with the same schema and filters
            page_size: Number of tables to return per page (default: 50, must be greater than 0)
            include_detailed_columns: Whether to include column metadata (default: True)

        Returns:
            A JSON-encoded string of an object containing:
            - tables: List of table information (as dictionaries)
            - next_page_token: Token for the next page, or None if no more pages
            - total_tables: Total number of tables matching the filters
        """
        future = self.executors.metadata.submit(
            self.list_tables,
            schema,
            like,
            not_like,
            page_token,
            page_size,
            include_detailed_columns,
        )
        return await asyncio.wrap_future(future)
