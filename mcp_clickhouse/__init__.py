import os

from .mcp_server import (
    create_clickhouse_client,
    list_databases,
    list_tables,
    run_select_query,
    create_chdb_client,
    run_chdb_select_query,
    chdb_initial_prompt,
    table_pagination_cache,
    fetch_table_names_from_system,
    get_paginated_table_data,
    create_page_token,
)


if os.getenv("MCP_CLICKHOUSE_TRUSTSTORE_DISABLE", None) != "1":
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass

__all__ = [
    "list_databases",
    "list_tables",
    "run_select_query",
    "create_clickhouse_client",
    "create_chdb_client",
    "run_chdb_select_query",
    "chdb_initial_prompt",
    "table_pagination_cache",
    "fetch_table_names_from_system",
    "get_paginated_table_data",
    "create_page_token",
]
