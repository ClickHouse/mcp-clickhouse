"""pgvector MCP service implementation."""

import logging
import concurrent.futures
from typing import Optional

from fastmcp import FastMCP
from fastmcp.tools import Tool
from fastmcp.prompts import Prompt
from fastmcp.exceptions import ToolError

from ..config import get_pgvector_config, get_mcp_config
from .prompts import PGVECTOR_PROMPT

logger = logging.getLogger("mcp-clickhouse.pgvector")

# Query executor
QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)


def create_pgvector_client():
    """Create PostgreSQL client connection with pgvector support."""
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError:
        raise ImportError(
            "psycopg2 is not installed. Install it with: pip install psycopg2-binary"
        )

    if not get_pgvector_config().enabled:
        raise ValueError("pgvector is not enabled. Set PGVECTOR_ENABLED=true to enable it.")

    client_config = get_pgvector_config().get_client_config()
    logger.info(
        f"Creating PostgreSQL client connection to {client_config['host']}:{client_config['port']} "
        f"as user {client_config['user']} (database={client_config['database']}, "
        f"sslmode={client_config['sslmode']})"
    )

    try:
        conn = psycopg2.connect(**client_config, cursor_factory=RealDictCursor)
        logger.info("Successfully connected to PostgreSQL server")
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to PostgreSQL: {str(e)}")
        raise


def execute_pgvector_query(query: str):
    """Execute query on PostgreSQL with pgvector support."""
    conn = None
    cursor = None
    try:
        conn = create_pgvector_client()
        cursor = conn.cursor()
        cursor.execute(query)

        # Check if query returns results
        if cursor.description:
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            # Convert RealDictRow to regular dict
            rows = [dict(row) for row in rows]
            logger.info(f"Query returned {len(rows)} rows")
            return {"columns": columns, "rows": rows}
        else:
            # For non-SELECT queries
            conn.commit()
            return {"message": "Query executed successfully", "rowcount": cursor.rowcount}
    except Exception as err:
        logger.error(f"Error executing query: {err}")
        if conn:
            conn.rollback()
        raise ToolError(f"Query execution failed: {str(err)}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def run_pgvector_select_query(query: str):
    """Run a SELECT query on PostgreSQL with pgvector support.
    
    PostgreSQL + pgvector extension
    
    Available Query Functions:
    1. Vector Similarity Search:
       - <-> : L2 distance (Euclidean) - for general vector comparison
       - <=> : Cosine distance - for text embeddings (recommended)
       - <#> : Inner product distance - for normalized vectors
       - Example: SELECT id, vec_col <=> '[0.1,0.2,0.3]' AS distance FROM tbl ORDER BY distance LIMIT 10
    
    2. Full Text Search:
       - to_tsvector('english', text_col): Convert text to search vector
       - to_tsquery('search & terms'): Create search query
       - @@ operator: Match operation
       - Example: SELECT id FROM tbl WHERE to_tsvector('english', text) @@ to_tsquery('machine & learning') LIMIT 10
    
    3. Hybrid Queries:
       - Combine vector search with full-text search
       - Example: SELECT id FROM tbl WHERE to_tsvector('english', text) @@ to_tsquery('AI') ORDER BY vec_col <=> '[...]' LIMIT 10
    
    Best Practices:
    - Use LIMIT to prevent large result sets
    - Filter with WHERE first, then perform vector search
    - Combine multiple search conditions for better results
    """
    logger.info(f"Executing pgvector SELECT query: {query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_pgvector_query, query)
        try:
            timeout_secs = get_mcp_config().query_timeout
            result = future.result(timeout=timeout_secs)
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"Query failed: {result['error']}")
                return {
                    "status": "error",
                    "message": f"Query failed: {result['error']}",
                }
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(f"Query timed out after {timeout_secs} seconds: {query}")
            future.cancel()
            raise ToolError(f"Query timed out after {timeout_secs} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in run_pgvector_select_query: {str(e)}")
        raise RuntimeError(f"Unexpected error during query execution: {str(e)}")


def list_pgvector_tables():
    """List all tables in PostgreSQL database"""
    logger.info("Listing all PostgreSQL tables")
    query = """
        SELECT 
            table_schema,
            table_name,
            table_type
        FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY table_schema, table_name;
    """
    try:
        result = execute_pgvector_query(query)
        logger.info(f"Found {len(result.get('rows', []))} tables")
        return result
    except Exception as e:
        logger.error(f"Error listing tables: {str(e)}")
        raise ToolError(f"Failed to list tables: {str(e)}")


def list_pgvector_vectors():
    """List all vector columns and their dimensions across all tables.
    
    Returns detailed vector column information:
    - Table schema and table name
    - Vector column name
    - Vector dimensions (e.g., vector(1536) means 1536-dimensional vector)
    
    Use this tool to identify vector columns and their dimensions before creating vector indexes.
    """
    logger.info("Listing all vector columns in PostgreSQL database")
    query = """
        SELECT 
            c.table_schema,
            c.table_name,
            c.column_name,
            c.data_type,
            c.udt_name,
            CASE 
                WHEN c.udt_name = 'vector' THEN 
                    substring(c.data_type from 'vector\\((\\d+)\\)')
                ELSE NULL
            END as vector_dimensions
        FROM information_schema.columns c
        WHERE c.udt_name = 'vector'
            AND c.table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY c.table_schema, c.table_name, c.column_name;
    """
    try:
        result = execute_pgvector_query(query)
        logger.info(f"Found {len(result.get('rows', []))} vector columns")
        return result
    except Exception as e:
        logger.error(f"Error listing vector columns: {str(e)}")
        raise ToolError(f"Failed to list vector columns: {str(e)}")


def search_similar_vectors(
    table_name: str,
    vector_column: str,
    query_vector: str,
    limit: int = 10,
    distance_function: str = "l2",
):
    """Perform similarity search using vector embeddings.
    
    Args:
        table_name: Name of the table to search
        vector_column: Name of the vector column
        query_vector: Query vector as string (e.g., '[1,2,3]')
        limit: Number of results to return (default: 10)
        distance_function: Distance function to use - 'l2', 'cosine', or 'inner_product' (default: 'l2')
    
    Distance Functions:
    - 'l2': L2/Euclidean distance (<-> operator) - for general vector comparison
    - 'cosine': Cosine distance (<=> operator) - for text embeddings
    - 'inner_product': Inner product distance (<#> operator) - for normalized vectors
    
    Note: Ensure indexes are created for vector columns for optimal performance.
    """
    logger.info(f"Performing vector similarity search on {table_name}.{vector_column}")
    
    # Map distance function to operator
    operators = {
        "l2": "<->",  # L2 distance (Euclidean)
        "cosine": "<=>",  # Cosine distance
        "inner_product": "<#>",  # Inner product (negative dot product)
    }
    
    if distance_function not in operators:
        raise ToolError(
            f"Invalid distance function '{distance_function}'. "
            f"Valid options: {', '.join(operators.keys())}"
        )
    
    operator = operators[distance_function]
    
    query = f"""
        SELECT *, {vector_column} {operator} '{query_vector}' AS distance
        FROM {table_name}
        ORDER BY {vector_column} {operator} '{query_vector}'
        LIMIT {limit};
    """
    
    try:
        result = execute_pgvector_query(query)
        logger.info(f"Vector search returned {len(result.get('rows', []))} results")
        return result
    except Exception as e:
        logger.error(f"Error performing vector search: {str(e)}")
        raise ToolError(f"Vector search failed: {str(e)}")


def pgvector_initial_prompt() -> str:
    """This prompt helps users understand how to interact with pgvector and perform operations."""
    return PGVECTOR_PROMPT


def register_tools(mcp: FastMCP):
    """Register pgvector tools to MCP instance."""
    mcp.add_tool(Tool.from_function(run_pgvector_select_query))
    mcp.add_tool(Tool.from_function(list_pgvector_tables))
    mcp.add_tool(Tool.from_function(list_pgvector_vectors))
    mcp.add_tool(Tool.from_function(search_similar_vectors))
    
    pgvector_prompt = Prompt.from_function(
        pgvector_initial_prompt,
        name="pgvector_initial_prompt",
        description="This prompt helps users understand how to interact with pgvector and perform operations.",
    )
    mcp.add_prompt(pgvector_prompt)
    logger.info("pgvector tools and prompts registered")

