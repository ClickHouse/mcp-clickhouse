from fastmcp import FastMCP
from typing import Sequence
from dotenv import load_dotenv
import clickhouse_connect
import os


MCP_SERVER_NAME = "mcp-clickhouse"

load_dotenv()

deps = [
    "clickhouse-connect",
    "python-dotenv",
    "uvicorn",
]

mcp = FastMCP(MCP_SERVER_NAME, dependencies=deps)


@mcp.tool()
def list_databases():
    client = create_clickhouse_client()
    return client.command("SHOW DATABASES")


@mcp.tool()
def list_tables(database: str, like: str = None):
    client = create_clickhouse_client()
    query = f"SHOW TABLES FROM {database}"
    if like:
        query += f" LIKE '{like}'"
    result = client.command(query)

    def get_table_info(table):
        schema_query = f"DESCRIBE TABLE {database}.{table}"
        schema_result = client.query(schema_query)

        columns = []
        column_names = schema_result.column_names
        for row in schema_result.result_rows:
            column_dict = {}
            for i, col_name in enumerate(column_names):
                column_dict[col_name] = row[i]
            columns.append(column_dict)

        create_table_query = f"SHOW CREATE TABLE {database}.{table}"
        create_table_result = client.command(create_table_query)

        return {
            "database": database,
            "name": table,
            "columns": columns,
            "create_table_query": create_table_result,
        }

    tables = []
    if isinstance(result, str):
        # Single table result
        tables.append(get_table_info(result))
    elif isinstance(result, Sequence):
        # Multiple table results
        for table in result:
            tables.append(get_table_info(table))

    return tables


@mcp.tool()
def run_select_query(query: str):
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
        return rows
    except Exception as err:
        return f"error running query: {err}"


def create_clickhouse_client():
    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST"),
        port=os.getenv("CLICKHOUSE_PORT"),
        username=os.getenv("CLICKHOUSE_USER"),
        password=os.getenv("CLICKHOUSE_PASSWORD"),
    )