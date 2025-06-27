import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError
import asyncio
from mcp_clickhouse.mcp_server import mcp, create_clickhouse_client
from dotenv import load_dotenv
import json

# Load environment variables
load_dotenv()


@pytest.fixture(scope="module")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="module")
async def setup_test_database():
    """Set up test database and tables before running tests."""
    client = create_clickhouse_client()

    # Test database and table names
    test_db = "test_mcp_db"
    test_table = "test_table"
    test_table2 = "another_test_table"

    # Create test database
    client.command(f"CREATE DATABASE IF NOT EXISTS {test_db}")

    # Drop tables if they exist
    client.command(f"DROP TABLE IF EXISTS {test_db}.{test_table}")
    client.command(f"DROP TABLE IF EXISTS {test_db}.{test_table2}")

    # Create first test table with comments
    client.command(f"""
        CREATE TABLE {test_db}.{test_table} (
            id UInt32 COMMENT 'Primary identifier',
            name String COMMENT 'User name field',
            age UInt8 COMMENT 'User age',
            created_at DateTime DEFAULT now() COMMENT 'Record creation timestamp'
        ) ENGINE = MergeTree()
        ORDER BY id
        COMMENT 'Test table for MCP server testing'
    """)

    # Create second test table
    client.command(f"""
        CREATE TABLE {test_db}.{test_table2} (
            event_id UInt64,
            event_type String,
            timestamp DateTime
        ) ENGINE = MergeTree()
        ORDER BY (event_type, timestamp)
        COMMENT 'Event tracking table'
    """)

    # Insert test data
    client.command(f"""
        INSERT INTO {test_db}.{test_table} (id, name, age) VALUES
        (1, 'Alice', 30),
        (2, 'Bob', 25),
        (3, 'Charlie', 35),
        (4, 'Diana', 28)
    """)

    client.command(f"""
        INSERT INTO {test_db}.{test_table2} (event_id, event_type, timestamp) VALUES
        (1001, 'login', '2024-01-01 10:00:00'),
        (1002, 'logout', '2024-01-01 11:00:00'),
        (1003, 'login', '2024-01-01 12:00:00')
    """)

    yield test_db, test_table, test_table2

    # Cleanup after tests
    client.command(f"DROP DATABASE IF EXISTS {test_db}")


@pytest.fixture
def mcp_server():
    """Return the MCP server instance for testing."""
    return mcp


@pytest.mark.asyncio
async def test_list_databases(mcp_server, setup_test_database):
    """Test the list_databases tool."""
    test_db, _, _ = setup_test_database

    async with Client(mcp_server) as client:
        result = await client.call_tool("list_databases", {})

        # The result should be a list containing at least one item
        assert len(result) >= 1
        assert isinstance(result[0].text, str)

        # Parse the result text (it's a JSON list of database names)
        databases = json.loads(result[0].text)
        assert test_db in databases
        assert "system" in databases  # System database should always exist


@pytest.mark.asyncio
async def test_list_tables_basic(mcp_server, setup_test_database):
    """Test the list_tables tool without filters."""
    test_db, test_table, test_table2 = setup_test_database

    async with Client(mcp_server) as client:
        result = await client.call_tool("list_tables", {"database": test_db})

        assert len(result) >= 1
        tables = json.loads(result[0].text)

        # Should have exactly 2 tables
        assert len(tables) == 2

        # Get table names
        table_names = [table["name"] for table in tables]
        assert test_table in table_names
        assert test_table2 in table_names

        # Check table details
        for table in tables:
            assert table["database"] == test_db
            assert "columns" in table
            assert "total_rows" in table
            assert "engine" in table
            assert "comment" in table

            # Verify column information exists
            assert len(table["columns"]) > 0
            for column in table["columns"]:
                assert "name" in column
                assert "column_type" in column
                assert "comment" in column


@pytest.mark.asyncio
async def test_list_tables_with_like_filter(mcp_server, setup_test_database):
    """Test the list_tables tool with LIKE filter."""
    test_db, test_table, _ = setup_test_database

    async with Client(mcp_server) as client:
        # Test with LIKE filter
        result = await client.call_tool("list_tables", {"database": test_db, "like": "test_%"})

        tables_data = json.loads(result[0].text)

        # Handle both single dict and list of dicts
        if isinstance(tables_data, dict):
            tables = [tables_data]
        else:
            tables = tables_data

        assert len(tables) == 1
        assert tables[0]["name"] == test_table


@pytest.mark.asyncio
async def test_list_tables_with_not_like_filter(mcp_server, setup_test_database):
    """Test the list_tables tool with NOT LIKE filter."""
    test_db, _, test_table2 = setup_test_database

    async with Client(mcp_server) as client:
        # Test with NOT LIKE filter
        result = await client.call_tool("list_tables", {"database": test_db, "not_like": "test_%"})

        tables_data = json.loads(result[0].text)

        # Handle both single dict and list of dicts
        if isinstance(tables_data, dict):
            tables = [tables_data]
        else:
            tables = tables_data

        assert len(tables) == 1
        assert tables[0]["name"] == test_table2


@pytest.mark.asyncio
async def test_run_select_query_success(mcp_server, setup_test_database):
    """Test running a successful SELECT query."""
    test_db, test_table, _ = setup_test_database

    async with Client(mcp_server) as client:
        query = f"SELECT id, name, age FROM {test_db}.{test_table} ORDER BY id"
        result = await client.call_tool("run_select_query", {"query": query})

        query_result = json.loads(result[0].text)

        # Check structure
        assert "columns" in query_result
        assert "rows" in query_result

        # Check columns
        assert query_result["columns"] == ["id", "name", "age"]

        # Check rows
        assert len(query_result["rows"]) == 4
        assert query_result["rows"][0] == [1, "Alice", 30]
        assert query_result["rows"][1] == [2, "Bob", 25]
        assert query_result["rows"][2] == [3, "Charlie", 35]
        assert query_result["rows"][3] == [4, "Diana", 28]


@pytest.mark.asyncio
async def test_run_select_query_with_aggregation(mcp_server, setup_test_database):
    """Test running a SELECT query with aggregation."""
    test_db, test_table, _ = setup_test_database

    async with Client(mcp_server) as client:
        query = f"SELECT COUNT(*) as count, AVG(age) as avg_age FROM {test_db}.{test_table}"
        result = await client.call_tool("run_select_query", {"query": query})

        query_result = json.loads(result[0].text)

        assert query_result["columns"] == ["count", "avg_age"]
        assert len(query_result["rows"]) == 1
        assert query_result["rows"][0][0] == 4  # count
        assert query_result["rows"][0][1] == 29.5  # average age


@pytest.mark.asyncio
async def test_run_select_query_with_join(mcp_server, setup_test_database):
    """Test running a SELECT query with JOIN."""
    test_db, test_table, test_table2 = setup_test_database

    async with Client(mcp_server) as client:
        # Insert related data for join
        client_direct = create_clickhouse_client()
        client_direct.command(f"""
            INSERT INTO {test_db}.{test_table2} (event_id, event_type, timestamp) VALUES
            (2001, 'purchase', '2024-01-01 14:00:00')
        """)

        query = f"""
        SELECT
            COUNT(DISTINCT event_type) as event_types_count
        FROM {test_db}.{test_table2}
        """
        result = await client.call_tool("run_select_query", {"query": query})

        query_result = json.loads(result[0].text)
        assert query_result["rows"][0][0] == 3  # login, logout, purchase


@pytest.mark.asyncio
async def test_run_select_query_error(mcp_server, setup_test_database):
    """Test running a SELECT query that results in an error."""
    test_db, _, _ = setup_test_database

    async with Client(mcp_server) as client:
        # Query non-existent table
        query = f"SELECT * FROM {test_db}.non_existent_table"

        # Should raise ToolError
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("run_select_query", {"query": query})

        assert "Query execution failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_select_query_syntax_error(mcp_server):
    """Test running a SELECT query with syntax error."""
    async with Client(mcp_server) as client:
        # Invalid SQL syntax
        query = "SELECT FROM WHERE"

        # Should raise ToolError
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("run_select_query", {"query": query})

        assert "Query execution failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_table_metadata_details(mcp_server, setup_test_database):
    """Test that table metadata is correctly retrieved."""
    test_db, test_table, _ = setup_test_database

    async with Client(mcp_server) as client:
        result = await client.call_tool("list_tables", {"database": test_db})
        tables = json.loads(result[0].text)

        # Find our test table
        test_table_info = next(t for t in tables if t["name"] == test_table)

        # Check table comment
        assert test_table_info["comment"] == "Test table for MCP server testing"

        # Check engine info
        assert test_table_info["engine"] == "MergeTree"
        assert "MergeTree" in test_table_info["engine_full"]

        # Check row count
        assert test_table_info["total_rows"] == 4

        # Check columns and their comments
        columns_by_name = {col["name"]: col for col in test_table_info["columns"]}

        assert columns_by_name["id"]["comment"] == "Primary identifier"
        assert columns_by_name["id"]["column_type"] == "UInt32"

        assert columns_by_name["name"]["comment"] == "User name field"
        assert columns_by_name["name"]["column_type"] == "String"

        assert columns_by_name["age"]["comment"] == "User age"
        assert columns_by_name["age"]["column_type"] == "UInt8"

        assert columns_by_name["created_at"]["comment"] == "Record creation timestamp"
        assert columns_by_name["created_at"]["column_type"] == "DateTime"
        assert columns_by_name["created_at"]["default_expression"] == "now()"


@pytest.mark.asyncio
async def test_system_database_access(mcp_server):
    """Test that we can access system databases."""
    async with Client(mcp_server) as client:
        # List tables in system database
        result = await client.call_tool("list_tables", {"database": "system"})
        tables = json.loads(result[0].text)

        # System database should have many tables
        assert len(tables) > 10

        # Check for some common system tables
        table_names = [t["name"] for t in tables]
        assert "tables" in table_names
        assert "columns" in table_names
        assert "databases" in table_names


@pytest.mark.asyncio
async def test_concurrent_queries(mcp_server, setup_test_database):
    """Test running multiple queries concurrently."""
    test_db, test_table, test_table2 = setup_test_database

    async with Client(mcp_server) as client:
        # Run multiple queries concurrently
        queries = [
            f"SELECT COUNT(*) FROM {test_db}.{test_table}",
            f"SELECT COUNT(*) FROM {test_db}.{test_table2}",
            f"SELECT MAX(id) FROM {test_db}.{test_table}",
            f"SELECT MIN(event_id) FROM {test_db}.{test_table2}",
        ]

        # Execute all queries concurrently
        results = await asyncio.gather(
            *[client.call_tool("run_select_query", {"query": query}) for query in queries]
        )

        # Verify all queries succeeded
        assert len(results) == 4

        # Check each result
        for i, result in enumerate(results):
            query_result = json.loads(result[0].text)
            assert "rows" in query_result
            assert len(query_result["rows"]) == 1
