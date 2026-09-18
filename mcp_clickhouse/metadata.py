import asyncio
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from functools import wraps
from typing import Annotated, Any, List, Optional

from cachetools import TTLCache
from clickhouse_connect.driver.binding import format_query_value
from fastmcp.exceptions import ToolError
from pydantic import Field

from mcp_clickhouse import clients
from mcp_clickhouse.executors import _Executors
from mcp_clickhouse.serialization import _serialize_tool_result


logger = logging.getLogger("mcp-clickhouse")
_PAGE_TOKEN_EXPIRES_AT = "_expires_at"
_CLAIM_PAGE_TOKEN_IN_WORKER = object()


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


def result_to_table(query_columns, result) -> List[Table]:
    return [Table(**dict(zip(query_columns, row))) for row in result]


def result_to_column(query_columns, result) -> List[Column]:
    return [Column(**dict(zip(query_columns, row))) for row in result]


@dataclass(frozen=True)
class _PendingPageToken:
    token: str
    state: dict[str, Any]


@dataclass(frozen=True)
class _PreparedListTablesResult:
    response: str
    pending_page_token: Optional[_PendingPageToken]


def fetch_table_names_from_system(
    client,
    database: str,
    like: Optional[str] = None,
    not_like: Optional[str] = None,
) -> List[str]:
    """Get list of table names from system.tables.

    Args:
        client: ClickHouse client
        database: Database name
        like: Optional pattern to filter table names (LIKE)
        not_like: Optional pattern to filter out table names (NOT LIKE)

    Returns:
        List of table names
    """
    query = f"SELECT name FROM system.tables WHERE database = {format_query_value(database)}"
    if like:
        query += f" AND name LIKE {format_query_value(like)}"

    if not_like:
        query += f" AND name NOT LIKE {format_query_value(not_like)}"

    result = client.query(query)
    table_names = [row[0] for row in result.result_rows]
    return table_names


def get_paginated_table_data(
    client,
    database: str,
    table_names: List[str],
    start_idx: int,
    page_size: int,
    include_detailed_columns: bool = True,
) -> tuple[List[Table], int, bool]:
    """Get detailed information for a page of tables.

    Args:
        client: ClickHouse client
        database: Database name
        table_names: List of all table names to paginate
        start_idx: Starting index for pagination
        page_size: Number of tables per page
        include_detailed_columns: Whether to include detailed column metadata (default: True)

    Returns:
        Tuple of (list of Table objects, end index, has more pages)
    """
    end_idx = min(start_idx + page_size, len(table_names))
    current_page_table_names = table_names[start_idx:end_idx]

    if not current_page_table_names:
        return [], end_idx, False

    query = f"""
        SELECT database, name, engine, create_table_query, dependencies_database,
               dependencies_table, engine_full, sorting_key, primary_key, total_rows,
               total_bytes, total_bytes_uncompressed, parts, active_parts, total_marks, comment
        FROM system.tables
        WHERE database = {format_query_value(database)}
        AND name IN ({", ".join(format_query_value(name) for name in current_page_table_names)})
    """

    result = client.query(query)
    tables = result_to_table(result.column_names, result.result_rows)

    if include_detailed_columns:
        for table in tables:
            column_data_query = f"""
                SELECT database, table, name, type AS column_type, default_kind, default_expression, comment
                FROM system.columns
                WHERE database = {format_query_value(database)}
                AND table = {format_query_value(table.name)}
            """
            column_data_query_result = client.query(column_data_query)
            table.columns = result_to_column(
                column_data_query_result.column_names,
                column_data_query_result.result_rows,
            )
    else:
        for table in tables:
            table.columns = []

    return tables, end_idx, end_idx < len(table_names)


def _prepare_page_token(
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    table_names: List[str],
    end_idx: int,
    include_detailed_columns: bool,
) -> _PendingPageToken:
    """Prepare a page token without making it available to clients."""
    return _PendingPageToken(
        token=str(uuid.uuid4()),
        state={
            "database": database,
            "like": like,
            "not_like": not_like,
            "table_names": table_names,
            "start_idx": end_idx,
            "include_detailed_columns": include_detailed_columns,
        },
    )


def _list_tables_impl(
    client,
    database: str,
    like: Optional[str],
    not_like: Optional[str],
    page_token: Optional[str],
    page_size: int,
    include_detailed_columns: bool,
    claimed_page_state: object = None,
) -> _PreparedListTablesResult:
    """Inner implementation of list_tables, separated for retry logic."""
    if claimed_page_state is not None:
        table_names = claimed_page_state["table_names"]
        start_idx = claimed_page_state["start_idx"]

        tables, end_idx, has_more = get_paginated_table_data(
            client,
            database,
            table_names,
            start_idx,
            page_size,
            include_detailed_columns,
        )

        pending_page_token = None
        if has_more:
            pending_page_token = _prepare_page_token(
                database, like, not_like, table_names, end_idx, include_detailed_columns
            )
        next_page_token = pending_page_token.token if pending_page_token else None

        logger.info(
            "Returned page with %s tables (total: %s), next_page_token=%s",
            len(tables),
            len(table_names),
            next_page_token,
        )
        return _PreparedListTablesResult(
            response=_serialize_tool_result({
                "tables": [asdict(table) for table in tables],
                "next_page_token": next_page_token,
                "total_tables": len(table_names),
            }),
            pending_page_token=pending_page_token,
        )

    table_names = fetch_table_names_from_system(client, database, like, not_like)

    start_idx = 0
    tables, end_idx, has_more = get_paginated_table_data(
        client,
        database,
        table_names,
        start_idx,
        page_size,
        include_detailed_columns,
    )

    pending_page_token = None
    if has_more:
        pending_page_token = _prepare_page_token(
            database, like, not_like, table_names, end_idx, include_detailed_columns
        )
    next_page_token = pending_page_token.token if pending_page_token else None

    logger.info(
        "Found %s tables, returning %s with next_page_token=%s",
        len(table_names),
        len(tables),
        next_page_token,
    )

    return _PreparedListTablesResult(
        response=_serialize_tool_result({
            "tables": [asdict(table) for table in tables],
            "next_page_token": next_page_token,
            "total_tables": len(table_names),
        }),
        pending_page_token=pending_page_token,
    )


class _Metadata:
    """Keep metadata resources and pagination state local to one server assembly."""

    def __init__(self, executors: _Executors, clickhouse_clients: clients._ClickHouseClients):
        self.executors = executors
        self.clients = clickhouse_clients
        # Store pagination state for list_tables with an absolute 1-hour expiry.
        self.table_pagination_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)  # 3600 seconds = 1 hour
        self.table_pagination_cache_lock = threading.Lock()

    def list_databases(self) -> str:
        """List available ClickHouse databases"""
        return self._list_databases_with_config(clients._resolve_client_config())

    def _list_databases_with_config(self, config: dict[str, Any]) -> str:
        """List databases with a resolved client configuration."""
        logger.info("Listing all databases")

        for attempt in range(2):
            entry = None
            try:
                entry = self.clients._acquire_clickhouse_client(config)
                client = entry.client
                result = client.command("SHOW DATABASES")
                break
            except Exception as err:
                if attempt == 0 and clients._is_connection_error(err):
                    logger.warning("list_databases connection error, retrying: %s", err)
                    if entry is not None:
                        self.clients._evict_cached_client(config, entry.client)
                    continue
                raise
            finally:
                if entry is not None:
                    self.clients._release_client_entry(entry)

        # Convert newline-separated string to list and trim whitespace
        if isinstance(result, str):
            databases = [db.strip() for db in result.strip().split("\n")]
        else:
            databases = [result]

        logger.info(f"Found {len(databases)} databases")
        return _serialize_tool_result(databases)

    @wraps(list_databases)
    async def list_databases_async(self) -> str:
        overrides = await clients._get_client_config_overrides_for_tool()
        config = clients._resolve_client_config(overrides)
        future = self.executors.metadata.submit(self._list_databases_with_config, config)
        return await asyncio.wrap_future(future)

    def create_page_token(
        self,
        database: str,
        like: Optional[str],
        not_like: Optional[str],
        table_names: List[str],
        end_idx: int,
        include_detailed_columns: bool,
    ) -> str:
        """Create a new page token and store it in the cache.

        Args:
            database: Database name
            like: LIKE pattern used to filter tables
            not_like: NOT LIKE pattern used to filter tables
            table_names: List of all table names
            end_idx: Index to start from for the next page
            include_detailed_columns: Whether to include detailed column metadata

        Returns:
            New page token
        """
        pending_page_token = _prepare_page_token(
            database,
            like,
            not_like,
            table_names,
            end_idx,
            include_detailed_columns,
        )
        self._commit_page_token(pending_page_token)
        return pending_page_token.token

    def _commit_page_token(self, pending_page_token: _PendingPageToken) -> None:
        """Make a prepared page token available for one hour."""
        with self.table_pagination_cache_lock:
            expires_at = self.table_pagination_cache.timer() + self.table_pagination_cache.ttl
            state = dict(pending_page_token.state)
            state[_PAGE_TOKEN_EXPIRES_AT] = expires_at
            self.table_pagination_cache[pending_page_token.token] = state

    def _claim_page_token_for_request(
        self,
        page_token: str,
        database: str,
        like: Optional[str],
        not_like: Optional[str],
        include_detailed_columns: bool,
    ) -> Optional[dict[str, Any]]:
        """Claim a matching page token and leave a mismatched token untouched."""
        mismatch = False
        with self.table_pagination_cache_lock:
            state = self.table_pagination_cache.get(page_token)
            if state is None:
                return None
            expires_at = state.get(_PAGE_TOKEN_EXPIRES_AT)
            if expires_at is not None and expires_at <= self.table_pagination_cache.timer():
                self.table_pagination_cache.pop(page_token, None)
                return None
            cached_include_detailed = state.get("include_detailed_columns", True)
            mismatch = (
                state["database"] != database
                or state["like"] != like
                or state["not_like"] != not_like
                or cached_include_detailed != include_detailed_columns
            )
            if not mismatch:
                return self.table_pagination_cache.pop(page_token)

        logger.warning(
            "Page token %s is for a different database, filter, or metadata setting. "
            "Ignoring token and starting from beginning.",
            page_token,
        )
        return None

    def _restore_page_token(self, page_token: str, state: dict[str, Any]) -> None:
        """Restore a claimed pagination token unless another value already exists."""
        with self.table_pagination_cache_lock:
            expires_at = state.get(_PAGE_TOKEN_EXPIRES_AT)
            if expires_at is not None and expires_at <= self.table_pagination_cache.timer():
                return
            self.table_pagination_cache.setdefault(page_token, state)

    def list_tables(
        self,
        database: str,
        like: Optional[str] = None,
        not_like: Optional[str] = None,
        page_token: Optional[str] = None,
        page_size: Annotated[int, Field(gt=0)] = 50,
        include_detailed_columns: bool = True,
    ) -> str:
        """List available ClickHouse tables in a database, including schema, comment,
        row count, and column count.

        Integers outside [-9007199254740991, 9007199254740991] in table metadata are
        returned as decimal strings.
        Pagination tokens are single-use and retained for up to one hour.

        Args:
            database: The database to list tables from
            like: Optional LIKE pattern to filter table names
            not_like: Optional NOT LIKE pattern to exclude table names
            page_token: Single-use token from a previous call, retained for up to one hour
            page_size: Number of tables to return per page (default: 50, must be greater than 0)
            include_detailed_columns: Whether to include detailed column metadata (default: True).
                When False, the columns array will be empty but create_table_query still contains
                all column information. This reduces payload size for large schemas.

        Returns:
            A JSON-encoded string of an object containing:
            - tables: List of table information (as dictionaries)
            - next_page_token: Token for the next page, or None if no more pages
            - total_tables: Total number of tables matching the filters
        """
        if page_size <= 0:
            raise ToolError("page_size must be greater than 0")

        return self._list_tables_with_config(
            clients._resolve_client_config(),
            database,
            like,
            not_like,
            page_token,
            page_size,
            include_detailed_columns,
        )

    def _list_tables_with_config(
        self,
        config: dict[str, Any],
        database: str,
        like: Optional[str],
        not_like: Optional[str],
        page_token: Optional[str],
        page_size: int,
        include_detailed_columns: bool,
        claimed_page_state: object = _CLAIM_PAGE_TOKEN_IN_WORKER,
    ) -> str | _PreparedListTablesResult:
        """List tables with a resolved client configuration."""
        if page_size <= 0:
            raise ToolError("page_size must be greater than 0")

        owns_page_token_transaction = claimed_page_state is _CLAIM_PAGE_TOKEN_IN_WORKER
        if owns_page_token_transaction:
            claimed_page_state = (
                self._claim_page_token_for_request(
                    page_token,
                    database,
                    like,
                    not_like,
                    include_detailed_columns,
                )
                if page_token
                else None
            )

        logger.info(
            "Listing tables in database '%s' with like=%s, not_like=%s, "
            "page_token=%s, page_size=%s, include_detailed_columns=%s",
            database,
            like,
            not_like,
            page_token,
            page_size,
            include_detailed_columns,
        )

        try:
            for attempt in range(2):
                entry = None
                try:
                    entry = self.clients._acquire_clickhouse_client(config)
                    client = entry.client
                    prepared_result = _list_tables_impl(
                        client,
                        database,
                        like,
                        not_like,
                        page_token,
                        page_size,
                        include_detailed_columns,
                        claimed_page_state,
                    )
                    break
                except Exception as err:
                    if attempt == 0 and clients._is_connection_error(err):
                        logger.warning("list_tables connection error, retrying: %s", err)
                        if entry is not None:
                            self.clients._evict_cached_client(config, entry.client)
                        continue
                    raise
                finally:
                    if entry is not None:
                        self.clients._release_client_entry(entry)
        except BaseException:
            if owns_page_token_transaction and page_token and claimed_page_state is not None:
                self._restore_page_token(page_token, claimed_page_state)
            raise

        try:
            if not owns_page_token_transaction:
                return prepared_result
            if prepared_result.pending_page_token is not None:
                self._commit_page_token(prepared_result.pending_page_token)
            return prepared_result.response
        except BaseException:
            if owns_page_token_transaction and page_token and claimed_page_state is not None:
                self._restore_page_token(page_token, claimed_page_state)
            raise

    @wraps(list_tables)
    async def list_tables_async(
        self,
        database: str,
        like: Optional[str] = None,
        not_like: Optional[str] = None,
        page_token: Optional[str] = None,
        page_size: Annotated[int, Field(gt=0)] = 50,
        include_detailed_columns: bool = True,
    ) -> str:
        overrides = await clients._get_client_config_overrides_for_tool()
        config = clients._resolve_client_config(overrides)
        claimed_page_state = (
            self._claim_page_token_for_request(
                page_token,
                database,
                like,
                not_like,
                include_detailed_columns,
            )
            if page_token
            else None
        )
        completed = False
        try:
            future = self.executors.metadata.submit(
                self._list_tables_with_config,
                config,
                database,
                like,
                not_like,
                page_token,
                page_size,
                include_detailed_columns,
                claimed_page_state,
            )
            prepared_result = await asyncio.wrap_future(future)
            if isinstance(prepared_result, str):
                completed = True
                return prepared_result
            if prepared_result.pending_page_token is not None:
                self._commit_page_token(prepared_result.pending_page_token)
            completed = True
            return prepared_result.response
        finally:
            if not completed and page_token and claimed_page_state is not None:
                self._restore_page_token(page_token, claimed_page_state)
