import os

from .mcp_server import (
    chdb_initial_prompt,
    create_chdb_client,
    create_clickhouse_client,
    list_databases,
    list_tables,
    run_chdb_select_query,
    run_select_query,
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
]
