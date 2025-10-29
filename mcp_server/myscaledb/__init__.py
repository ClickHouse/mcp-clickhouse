"""MyScaleDB MCP service module."""

from .server import (
    create_myscale_client,
    list_databases,
    list_tables,
    run_select_query,
    register_tools,
)
from .prompts import MYSCALEDB_PROMPT

__all__ = [
    "create_myscale_client",
    "list_databases",
    "list_tables",
    "run_select_query",
    "register_tools",
    "MYSCALEDB_PROMPT",
]
