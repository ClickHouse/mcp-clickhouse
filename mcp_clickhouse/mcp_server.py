import logging
from typing import Sequence
import concurrent.futures
import atexit

import clickhouse_connect
from clickhouse_connect.driver.binding import quote_identifier, format_query_value
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from mcp_clickhouse.mcp_env import get_config

MCP_SERVER_NAME = "mcp-clickhouse"

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(MCP_SERVER_NAME)

QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)
atexit.register(lambda: QUERY_EXECUTOR.shutdown(wait=True))
SELECT_QUERY_TIMEOUT_SECS = 30

load_dotenv()

deps = [
    "clickhouse-connect",
    "python-dotenv",
    "uvicorn",
    "pip-system-certs",
]

mcp = FastMCP(MCP_SERVER_NAME, dependencies=deps)


@mcp.tool()
def list_databases():
    """List available ClickHouse databases"""
    logger.info("Listing all databases")
    client = create_clickhouse_client()
    result = client.command("SHOW DATABASES")
    logger.info(f"Found {len(result) if isinstance(result, list) else 1} databases")
    return result


@mcp.tool()
def list_tables(database: str, like: str = None):
    """List available ClickHouse tables in a database, including schema, comment,
    row count, and column count."""
    logger.info(f"Listing tables in database '{database}'")
    client = create_clickhouse_client()
    query = f"SHOW TABLES FROM {quote_identifier(database)}"
    if like:
        query += f" LIKE {format_query_value(like)}"
    result = client.command(query)

    # Get all table comments in one query
    table_comments_query = f"SELECT name, comment FROM system.tables WHERE database = {format_query_value(database)}"
    table_comments_result = client.query(table_comments_query)
    table_comments = {row[0]: row[1] for row in table_comments_result.result_rows}

    # Get all column comments in one query
    column_comments_query = f"SELECT table, name, comment FROM system.columns WHERE database = {format_query_value(database)}"
    column_comments_result = client.query(column_comments_query)
    column_comments = {}
    for row in column_comments_result.result_rows:
        table, col_name, comment = row
        if table not in column_comments:
            column_comments[table] = {}
        column_comments[table][col_name] = comment

    def get_table_info(table):
        logger.info(f"Getting schema info for table {database}.{table}")
        schema_query = f"DESCRIBE TABLE {quote_identifier(database)}.{quote_identifier(table)}"
        schema_result = client.query(schema_query)

        columns = []
        column_names = schema_result.column_names
        for row in schema_result.result_rows:
            column_dict = {}
            for i, col_name in enumerate(column_names):
                column_dict[col_name] = row[i]
            # Add comment from our pre-fetched comments
            if table in column_comments and column_dict['name'] in column_comments[table]:
                column_dict['comment'] = column_comments[table][column_dict['name']]
            else:
                column_dict['comment'] = None
            columns.append(column_dict)

        # Get row count and column count from the table
        row_count_query = f"SELECT count() FROM {quote_identifier(database)}.{quote_identifier(table)}"
        row_count_result = client.query(row_count_query)
        row_count = row_count_result.result_rows[0][0] if row_count_result.result_rows else 0
        column_count = len(columns)

        create_table_query = f"SHOW CREATE TABLE {database}.`{table}`"
        create_table_result = client.command(create_table_query)

        return {
            "database": database,
            "name": table,
            "comment": table_comments.get(table),
            "columns": columns,
            "create_table_query": create_table_result,
            "row_count": row_count,
            "column_count": column_count,
        }

    tables = []
    if isinstance(result, str):
        # Single table result
        for table in (t.strip() for t in result.split()):
            if table:
                tables.append(get_table_info(table))
    elif isinstance(result, Sequence):
        # Multiple table results
        for table in result:
            tables.append(get_table_info(table))

    logger.info(f"Found {len(tables)} tables")
    return tables


def execute_query(query: str):
    client = create_clickhouse_client()
    try:
        res = client.query(query, settings={"readonly": 1})
        column_names = res.column_names
        rows = []
        for row in res.result_rows:
            row_dict = {}
            for i, col_name in enumerate(column_names):
                row_dict[col_name] = row[i]
            rows.append(row_dict)
        logger.info(f"Query returned {len(rows)} rows")
        return rows
    except Exception as err:
        logger.error(f"Error executing query: {err}")
        # Return a structured dictionary rather than a string to ensure proper serialization
        # by the MCP protocol. String responses for errors can cause BrokenResourceError.
        return {"error": str(err)}


@mcp.tool()
def run_select_query(query: str):
    """Run a SELECT query in a ClickHouse database"""
    logger.info(f"Executing SELECT query: {query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_query, query)
        try:
            result = future.result(timeout=SELECT_QUERY_TIMEOUT_SECS)
            # Check if we received an error structure from execute_query
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"Query failed: {result['error']}")
                # MCP requires structured responses; string error messages can cause
                # serialization issues leading to BrokenResourceError
                return {"status": "error", "message": f"Query failed: {result['error']}"}
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds: {query}")
            future.cancel()
            # Return a properly structured response for timeout errors
            return {"status": "error", "message": f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds"}
    except Exception as e:
        logger.error(f"Unexpected error in run_select_query: {str(e)}")
        # Catch all other exceptions and return them in a structured format
        # to prevent MCP serialization failures
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}


def create_clickhouse_client():
    client_config = get_config().get_client_config()
    logger.info(
        f"Creating ClickHouse client connection to {client_config['host']}:{client_config['port']} "
        f"as {client_config['username']} "
        f"(secure={client_config['secure']}, verify={client_config['verify']}, "
        f"connect_timeout={client_config['connect_timeout']}s, "
        f"send_receive_timeout={client_config['send_receive_timeout']}s)"
    )

    try:
        client = clickhouse_connect.get_client(**client_config)
        # Test the connection
        version = client.server_version
        logger.info(f"Successfully connected to ClickHouse server version {version}")
        return client
    except Exception as e:
        logger.error(f"Failed to connect to ClickHouse: {str(e)}")
        raise
