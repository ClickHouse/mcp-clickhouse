import os
import warnings

from . import mcp_server as _mcp_server
from .mcp_server import (
    create_clickhouse_client,
    list_databases,
    list_tables,
    run_query,
    create_chdb_client,
    run_chdb_select_query,
    chdb_initial_prompt,
)


if os.getenv("MCP_CLICKHOUSE_TRUSTSTORE_DISABLE", None) != "1":
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass

# Internal pagination helpers that were historically re-exported from the
# package namespace. They stay importable for one release with a
# DeprecationWarning and leave __all__ in the next minor release.
_DEPRECATED_INTERNALS = (
    "table_pagination_cache",
    "fetch_table_names_from_system",
    "get_paginated_table_data",
    "create_page_token",
)


def __getattr__(name: str):
    if name in _DEPRECATED_INTERNALS:
        warnings.warn(
            f"mcp_clickhouse.{name} is an internal pagination helper and will be removed "
            "from the mcp_clickhouse package namespace in the next minor release. Use the "
            "list_tables tool or helper instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return getattr(_mcp_server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    # Keep the deprecated names visible to dir(), help(), and inspect while they
    # are still exported, so the deprecation is discoverable.
    return sorted(set(globals()) | set(_DEPRECATED_INTERNALS))


__all__ = [
    "list_databases",
    "list_tables",
    "run_query",
    "create_clickhouse_client",
    "create_chdb_client",
    "run_chdb_select_query",
    "chdb_initial_prompt",
    "table_pagination_cache",
    "fetch_table_names_from_system",
    "get_paginated_table_data",
    "create_page_token",
]
