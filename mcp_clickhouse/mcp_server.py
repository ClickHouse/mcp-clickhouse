import logging
from typing import Sequence

import clickhouse_connect
from clickhouse_connect.driver.binding import quote_identifier, format_query_value
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from mcp_clickhouse.mcp_env import config

MCP_SERVER_NAME = "mcp-clickhouse"

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(MCP_SERVER_NAME)

load_dotenv()

deps = [
    "clickhouse-connect",
    "python-dotenv",
    "uvicorn",
    "pip-system-certs",
]

mcp = FastMCP(MCP_SERVER_NAME, dependencies=deps)


@mcp.tool(
    description="Lists all available databases in the ClickHouse server. Use this tool to get a complete list of databases before exploring their tables. No parameters required."
)
def list_databases():
    logger.info("Listing all databases")
    client = create_clickhouse_client()
    result = client.command("SHOW DATABASES")
    logger.info(
        f"Found {len(result) if isinstance(result, list) else 1} databases")
    return result


@mcp.tool(
    description="Lists tables in a ClickHouse database with detailed schema information. "
    "Provides complete table structure including columns, types, and creation statements. "
    "Use the 'like' parameter to filter results with SQL LIKE pattern."
)
def list_tables(database: str, like: str = None):
    logger.info(f"Listing tables in database '{database}'")
    client = create_clickhouse_client()
    query = f"SHOW TABLES FROM {quote_identifier(database)}"
    if like:
        query += f" LIKE {format_query_value(like)}"
    result = client.command(query)

    # Get all table comments in one query
    table_comments_query = f"SELECT name, comment FROM system.tables WHERE database = {format_query_value(database)}"
    table_comments_result = client.query(table_comments_query)
    table_comments = {row[0]: row[1]
                      for row in table_comments_result.result_rows}

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

        create_table_query = f"SHOW CREATE TABLE {database}.`{table}`"
        create_table_result = client.command(create_table_query)

        return {
            "database": database,
            "name": table,
            "comment": table_comments.get(table),
            "columns": columns,
            "create_table_query": create_table_result,
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


@mcp.tool(
    description="Executes a SELECT query against the ClickHouse database. "
    "Use for custom data retrieval with your own SQL. "
    "Queries are executed in read-only mode for safety. "
    "Format your query without specifying database names in SQL."
)
def run_select_query(query: str):
    logger.info(f"Executing SELECT query: {query}")
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
        return f"error running query: {err}"


@mcp.tool(
    description="Retrieves a random sample of rows from a table using ORDER BY RAND(). "
    "Perfect for data exploration and quick analysis. "
    "Limit parameter capped at 10 rows. "
    "Use the where parameter for filtering specific data patterns."
)
def get_table_sample(database: str, table: str, columns: str = "*", limit: int = 5, where: str = None):
    """Retrieves a random sample of rows from a table with ORDER BY RAND()

    Args:
        database: The database containing the table
        table: The table to sample data from
        columns: Comma-separated list of columns to retrieve (default: "*" for all columns)
        limit: Maximum number of rows to return (default: 5, max: 10)
        where: Optional WHERE clause to filter the data

    Returns:
        List of dictionaries, each representing a random row from the table

    Raises:
        ValueError: If limit is > 10 or < 1
        ConnectionError: If there's an issue connecting to ClickHouse
        ClickHouseError: If there's an error executing the query
    """
    # Validate limit
    if limit > 10:
        logger.warning(
            f"Requested limit {limit} exceeds maximum of 10, using 10 instead")
        limit = 10
    elif limit < 1:
        logger.warning(
            f"Requested limit {limit} is less than 1, using 1 instead")
        limit = 1

    logger.info(f"Sampling {limit} random rows from {database}.{table}")
    client = create_clickhouse_client()

    try:
        # Build the query
        query = f"SELECT {columns} FROM {quote_identifier(database)}.{quote_identifier(table)}"

        # Add WHERE clause if provided
        if where:
            query += f" WHERE {where}"

        # Add random ordering and limit
        query += f" ORDER BY rand() LIMIT {limit}"

        logger.info(f"Executing sampling query: {query}")

        # Execute query with readonly setting for safety
        res = client.query(query, settings={"readonly": 1})
        column_names = res.column_names
        rows = []

        for row in res.result_rows:
            row_dict = {}
            for i, col_name in enumerate(column_names):
                row_dict[col_name] = row[i]
            rows.append(row_dict)

        logger.info(f"Sample query returned {len(rows)} rows")
        return rows
    except Exception as err:
        logger.error(f"Error executing sample query: {err}")
        return f"error running sample query: {err}"


def create_clickhouse_client():
    client_config = config.get_client_config()
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
        logger.info(
            f"Successfully connected to ClickHouse server version {version}")
        return client
    except Exception as e:
        logger.error(f"Failed to connect to ClickHouse: {str(e)}")
        raise
