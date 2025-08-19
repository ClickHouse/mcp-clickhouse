import unittest
import json

from dotenv import load_dotenv
from fastmcp.exceptions import ToolError

from mcp_clickhouse import create_clickhouse_client, list_clickhouse_tenants, list_chdb_tenants, list_databases, list_tables, run_select_query 

load_dotenv()

class TestClickhouseTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Set up the environment before tests."""
        cls.client = create_clickhouse_client("example")

        # Prepare test database and table
        cls.test_db = "test_tool_db"
        cls.test_table = "test_table"
        cls.client.command(f"CREATE DATABASE IF NOT EXISTS {cls.test_db}")

        # Drop table if exists to ensure clean state
        cls.client.command(f"DROP TABLE IF EXISTS {cls.test_db}.{cls.test_table}")

        # Create table with comments
        cls.client.command(f"""
            CREATE TABLE {cls.test_db}.{cls.test_table} (
                id UInt32 COMMENT 'Primary identifier',
                name String COMMENT 'User name field'
            ) ENGINE = MergeTree()
            ORDER BY id
            COMMENT 'Test table for unit testing'
        """)
        cls.client.command(f"""
            INSERT INTO {cls.test_db}.{cls.test_table} (id, name) VALUES (1, 'Alice'), (2, 'Bob')
        """)

    @classmethod
    def tearDownClass(cls):
        """Clean up the environment after tests."""
        cls.client.command(f"DROP DATABASE IF EXISTS {cls.test_db}")

    def test_list_clickhouse_tenants(self):
        tenants = list_clickhouse_tenants()
        self.assertIn("example", tenants)
        self.assertIn("default", tenants)
        self.assertEqual(len(tenants), 2)

    def test_list_chdb_tenants(self):
        tenants = list_chdb_tenants()
        self.assertIn("example", tenants)
        self.assertIn("default", tenants)
        self.assertEqual(len(tenants), 2)

    def test_list_databases_wrong_tenant(self):
        """Test listing tables with wrong tenant."""
        tenant = "wrong_tenant"
        with self.assertRaises(ToolError) as cm:
            list_databases(tenant)

        self.assertIn(
            f"List databases not performed for invalid tenant - '{tenant}'",
            str(cm.exception)
        )

    def test_list_tables_wrong_tenant(self):
        """Test listing tables with wrong tenant."""
        tenant = "wrong_tenant"
        with self.assertRaises(ToolError) as cm:
            list_tables(tenant, self.test_db)

        self.assertIn(
            f"List tables not performed for invalid tenant - '{tenant}'",
            str(cm.exception)
        )

    def test_run_select_query_wrong_tenant(self):
        """Test run select query with wrong tenant."""
        tenant = "wrong_tenant"
        query = f"SELECT * FROM {self.test_db}.{self.test_table}"
        with self.assertRaises(ToolError) as cm:
            run_select_query(tenant, query)

        self.assertIn(
            f"Query not performed for invalid tenant - '{tenant}'",
            str(cm.exception)
        )

    def test_list_databases(self):
        """Test listing databases."""
        result = list_databases("example")
        # Parse JSON response
        databases = json.loads(result)
        self.assertIn(self.test_db, databases)

    def test_list_tables_without_like(self):
        """Test listing tables without a 'LIKE' filter."""
        result = list_tables("example", self.test_db)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], self.test_table)

    def test_list_tables_with_like(self):
        """Test listing tables with a 'LIKE' filter."""
        result = list_tables("example", self.test_db, like=f"{self.test_table}%")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], self.test_table)

    def test_run_select_query_success(self):
        """Test running a SELECT query successfully."""
        query = f"SELECT * FROM {self.test_db}.{self.test_table}"
        result = run_select_query("example", query)
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0][0], 1)
        self.assertEqual(result["rows"][0][1], "Alice")

    def test_run_select_query_failure(self):
        """Test running a SELECT query with an error."""
        query = f"SELECT * FROM {self.test_db}.non_existent_table"

        # Should raise ToolError
        with self.assertRaises(ToolError) as context:
            run_select_query("example", query)

        self.assertIn("Query execution failed", str(context.exception))

    def test_table_and_column_comments(self):
        """Test that table and column comments are correctly retrieved."""
        result = list_tables("example", self.test_db)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)

        table_info = result[0]
        # Verify table comment
        self.assertEqual(table_info["comment"], "Test table for unit testing")

        # Get columns by name for easier testing
        columns = {col["name"]: col for col in table_info["columns"]}

        # Verify column comments
        self.assertEqual(columns["id"]["comment"], "Primary identifier")
        self.assertEqual(columns["name"]["comment"], "User name field")


if __name__ == "__main__":
    unittest.main()
