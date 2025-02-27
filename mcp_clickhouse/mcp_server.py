import logging
from typing import Dict, List, Sequence, Any, Optional, Annotated

import clickhouse_connect
from clickhouse_connect.driver.binding import quote_identifier, format_query_value
from clickhouse_connect.driver.exceptions import ClickHouseError
from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import Field

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
    description="Lists all available databases in the ClickHouse server"
)
def list_databases() -> List[str]:
    """Lists all available databases in the ClickHouse server.

    Returns:
        List[str]: A list of database names available on the server

    Raises:
        ConnectionError: If there's an issue connecting to the ClickHouse server
        ClickHouseError: If there's an error executing the query

    Examples:
        >>> list_databases()
        ["default", "system", "information_schema"]
    """
    logger.info("Listing all databases")
    try:
        client = create_clickhouse_client()
        result = client.command("SHOW DATABASES")
        logger.info(
            f"Found {len(result) if isinstance(result, list) else 1} databases")
        return result
    except ClickHouseError as e:
        logger.error(f"ClickHouse error when listing databases: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error when listing databases: {str(e)}")
        raise


@mcp.tool(
    description="Lists tables in a ClickHouse database with detailed schema information"
)
def list_tables(
    database: Annotated[str, Field(description="The name of the database to list tables from")],
    like: Annotated[Optional[str], Field(
        description="Optional pattern to filter table names (SQL LIKE syntax)", default=None)] = None
) -> List[Dict[str, Any]]:
    """Lists tables in a ClickHouse database with detailed schema information.

    Args:
        database: The name of the database to list tables from
        like: Optional pattern to filter table names (SQL LIKE syntax)

    Returns:
        List of tables with their schema information including columns and creation statements.
        Each table entry contains:
        - database: Database name
        - name: Table name
        - comment: Table comment if available
        - columns: List of column definitions with name, type, and other properties
        - create_table_query: CREATE TABLE statement

    Raises:
        ValueError: If the database name is invalid
        ConnectionError: If there's an issue connecting to ClickHouse
        ClickHouseError: If there's an error executing the query

    Examples:
        >>> list_tables("system")
        [{"database": "system", "name": "tables", "columns": [{"name": "database", "type": "String"}, {"name": "name", "type": "String"}]}]

        >>> list_tables("default", like="user%")
        [{"database": "default", "name": "users", "columns": [{"name": "id", "type": "UInt64"}, {"name": "name", "type": "String"}]}]
    """
    if not database:
        raise ValueError("Database name cannot be empty")

    logger.info(f"Listing tables in database '{database}'")
    try:
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

            create_table_query = f"SHOW CREATE TABLE {quote_identifier(database)}.{quote_identifier(table)}"
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
    except ClickHouseError as e:
        logger.error(f"ClickHouse error when listing tables: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error when listing tables: {str(e)}")
        raise


@mcp.tool(
    description="Executes a SELECT query against the ClickHouse database with a default limit of 5 rows"
)
def run_select_query(
    query: Annotated[str, Field(description="The SELECT query to execute. Must be a read-only query.")],
    limit: Annotated[int, Field(
        description="Maximum number of rows to return. Default is 5, must be between 1 and 1000.", default=5, ge=1, le=1000)] = 5
) -> List[Dict[str, Any]]:
    """Executes a SELECT query against the ClickHouse database with a default limit of 5 rows.

    Args:
        query: The SELECT query to execute. Must be a read-only query.
        limit: Maximum number of rows to return. Default is 5, must be between 1 and 1000.

    Returns:
        List of dictionaries, where each dictionary represents a row with column names as keys

    Raises:
        ValueError: If the query is not a SELECT query or limit is invalid
        ConnectionError: If there's an issue connecting to ClickHouse
        ClickHouseError: If there's an error executing the query

    Examples:
        >>> run_select_query("SELECT database, name FROM system.tables")
        [{"database": "system", "name": "tables"}, {"database": "system", "name": "columns"}]

        >>> run_select_query("SELECT database, name FROM system.tables", limit=10)
        [{"database": "system", "name": "tables"}, {"database": "system", "name": "columns"}]
    """
    if not query.strip().upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed")

    if limit < 1 or limit > 1000:
        raise ValueError("Limit must be between 1 and 1000")

    # Add LIMIT clause if not already present
    query = query.strip()
    if "LIMIT" not in query.upper():
        query += f" LIMIT {limit}"

    logger.info(f"Executing SELECT query with limit {limit}: {query}")
    try:
        client = create_clickhouse_client()
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
    except ClickHouseError as e:
        logger.error(f"ClickHouse error executing query: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error executing query: {str(e)}")
        raise


@mcp.tool(
    description="Retrieves a random sample of rows from a table with ORDER BY RAND()"
)
def sample_table(
    database: Annotated[str, Field(description="The database containing the table")],
    table: Annotated[str, Field(description="The table to sample from")],
    columns: Annotated[str, Field(
        description="Comma-separated list of columns to retrieve. Default is '*' (all columns).", default="*")] = "*",
    limit: Annotated[int, Field(
        description="Maximum number of rows to return. Default is 5, maximum is 10.", default=5, ge=1, le=10)] = 5,
    where: Annotated[Optional[str], Field(
        description="Optional WHERE clause to filter the data", default=None)] = None
) -> List[Dict[str, Any]]:
    """Retrieves a random sample of rows from a table using ORDER BY RAND().

    This function is useful for getting a representative sample of data from a table
    for exploration or analysis purposes. The rows are randomly selected using ClickHouse's
    RAND() function.

    Args:
        database: The database containing the table
        table: The table to sample from
        columns: Comma-separated list of columns to retrieve. Default is '*' (all columns).
        limit: Maximum number of rows to return. Default is 5, maximum is 10.
        where: Optional WHERE clause to filter the data

    Returns:
        List of dictionaries, where each dictionary represents a row with column names as keys

    Raises:
        ValueError: If parameters are invalid
        ConnectionError: If there's an issue connecting to ClickHouse
        ClickHouseError: If there's an error executing the query

    Examples:
        >>> sample_table("default", "users")
        [{"id": 42, "name": "Alice"}, {"id": 17, "name": "Bob"}]

        >>> sample_table("default", "events", columns="event_id, event_type, timestamp", limit=10, where="event_type = 'click'")
        [{"event_id": 123, "event_type": "click", "timestamp": "2023-01-01 12:34:56"}, 
         {"event_id": 456, "event_type": "click", "timestamp": "2023-01-02 10:11:12"}]
    """
    if not database:
        raise ValueError("Database name cannot be empty")

    if not table:
        raise ValueError("Table name cannot be empty")

    if limit < 1 or limit > 10:
        raise ValueError("Limit must be between 1 and 10")

    # Construct the query
    query = f"SELECT {columns} FROM {quote_identifier(database)}.{quote_identifier(table)}"

    if where:
        query += f" WHERE {where}"

    query += f" ORDER BY rand() LIMIT {limit}"

    logger.info(f"Sampling table {database}.{table} with limit {limit}")
    try:
        client = create_clickhouse_client()
        res = client.query(query, settings={"readonly": 1})
        column_names = res.column_names
        rows = []
        for row in res.result_rows:
            row_dict = {}
            for i, col_name in enumerate(column_names):
                row_dict[col_name] = row[i]
            rows.append(row_dict)
        logger.info(f"Sample returned {len(rows)} rows")
        return rows
    except ClickHouseError as e:
        logger.error(f"ClickHouse error sampling table: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error sampling table: {str(e)}")
        raise


def create_clickhouse_client():
    """Creates and returns a ClickHouse client connection.

    Returns:
        A configured ClickHouse client instance

    Raises:
        ConnectionError: If there's an issue connecting to ClickHouse
    """
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
        raise ConnectionError(f"Failed to connect to ClickHouse: {str(e)}")
