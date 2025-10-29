"""pgvector MCP 服务模块。"""

from .server import (
    create_pgvector_client,
    run_pgvector_select_query,
    list_pgvector_tables,
    list_pgvector_vectors,
    search_similar_vectors,
    pgvector_initial_prompt,
    register_tools,
)
from .prompts import PGVECTOR_PROMPT

__all__ = [
    "create_pgvector_client",
    "run_pgvector_select_query",
    "list_pgvector_tables",
    "list_pgvector_vectors",
    "search_similar_vectors",
    "pgvector_initial_prompt",
    "register_tools",
    "PGVECTOR_PROMPT",
]

