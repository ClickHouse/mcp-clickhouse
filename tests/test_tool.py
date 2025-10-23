import json
import os
import unittest
from unittest.mock import patch

from dotenv import load_dotenv
from fastmcp.exceptions import ToolError

from mcp_clickhouse import create_clickhouse_client, list_databases, list_tables, run_query

load_dotenv()


class TestClickhouseTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Set up the environment before tests."""
        cls.client = create_clickhouse_client()

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

    def test_list_databases(self):
        """Test listing databases."""
        result = list_databases()
        # Parse JSON response
        databases = json.loads(result)
        self.assertIn(self.test_db, databases)

    def test_list_tables_without_like(self):
        """Test listing tables without a 'LIKE' filter."""
        result = list_tables(self.test_db)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], self.test_table)

    def test_list_tables_with_like(self):
        """Test listing tables with a 'LIKE' filter."""
        result = list_tables(self.test_db, like=f"{self.test_table}%")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], self.test_table)

    def test_run_query_success(self):
        """Test running a SELECT query successfully."""
        query = f"SELECT * FROM {self.test_db}.{self.test_table}"
        result = run_query(query)
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0][0], 1)
        self.assertEqual(result["rows"][0][1], "Alice")

    def test_run_query_failure(self):
        """Test running a SELECT query with an error."""
        query = f"SELECT * FROM {self.test_db}.non_existent_table"

        # Should raise ToolError
        with self.assertRaises(ToolError) as context:
            run_query(query)

        self.assertIn("Query execution failed", str(context.exception))

    def test_table_and_column_comments(self):
        """Test that table and column comments are correctly retrieved."""
        result = list_tables(self.test_db)
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


@patch.dict(os.environ, {"CLICKHOUSE_READ_ONLY": "false", "CLICKHOUSE_ALLOW_DROP": "true"})
class TestClickhouseWriteMode(unittest.TestCase):
    """Tests for write mode functionality (CLICKHOUSE_READ_ONLY=false).

    Note: These tests use @patch.dict to temporarily set CLICKHOUSE_READ_ONLY=false
    and CLICKHOUSE_ALLOW_DROP=true without affecting other tests. This allows testing
    write operations in isolation.
    """

    @classmethod
    def setUpClass(cls):
        """Set up the environment before tests."""
        cls.client = create_clickhouse_client()
        cls.test_db = "test_write_mode_db"
        cls.test_table = "write_test_table"

        cls.client.command(f"DROP DATABASE IF EXISTS {cls.test_db}")
        cls.client.command(f"CREATE DATABASE {cls.test_db}")

    @classmethod
    def tearDownClass(cls):
        """Clean up the environment after tests."""
        cls.client.command(f"DROP DATABASE IF EXISTS {cls.test_db}")

    def test_insert_query(self):
        """Test that INSERT queries work when writes are enabled."""
        create_query = f"""
            CREATE TABLE {self.test_db}.{self.test_table} (
                id UInt32,
                value String
            ) ENGINE = MergeTree()
            ORDER BY id
        """
        result = run_query(create_query)
        self.assertIsInstance(result, dict)

        insert_query = f"""
            INSERT INTO {self.test_db}.{self.test_table} (id, value)
            VALUES (1, 'test_value')
        """
        result = run_query(insert_query)
        self.assertIsInstance(result, dict)

        select_query = f"SELECT * FROM {self.test_db}.{self.test_table}"
        result = run_query(select_query)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0][0], 1)
        self.assertEqual(result["rows"][0][1], "test_value")

        self.client.command(f"DROP TABLE {self.test_db}.{self.test_table}")

    def test_create_table_query(self):
        """Test that CREATE TABLE queries work when writes are enabled."""
        create_query = f"""
            CREATE TABLE {self.test_db}.ddl_test (
                id UInt32,
                name String
            ) ENGINE = MergeTree()
            ORDER BY id
        """
        result = run_query(create_query)
        self.assertIsInstance(result, dict)

        tables = list_tables(self.test_db)
        table_names = [t["name"] for t in tables]
        self.assertIn("ddl_test", table_names)

        self.client.command(f"DROP TABLE {self.test_db}.ddl_test")

    def test_alter_table_query(self):
        """Test that ALTER TABLE queries work when writes are enabled."""
        self.client.command(f"""
            CREATE TABLE {self.test_db}.alter_test (
                id UInt32
            ) ENGINE = MergeTree()
            ORDER BY id
        """)

        alter_query = f"""
            ALTER TABLE {self.test_db}.alter_test
            ADD COLUMN name String
        """
        result = run_query(alter_query)
        self.assertIsInstance(result, dict)

        tables = list_tables(self.test_db, like="alter_test")
        self.assertEqual(len(tables), 1)
        column_names = [col["name"] for col in tables[0]["columns"]]
        self.assertIn("name", column_names)

        self.client.command(f"DROP TABLE {self.test_db}.alter_test")


@patch.dict(os.environ, {"CLICKHOUSE_READ_ONLY": "false", "CLICKHOUSE_ALLOW_DROP": "true"})
class TestClickhouseDropProtection(unittest.TestCase):
    """Tests for DROP operation protection.

    These tests verify that DROP operations (DROP TABLE, DROP DATABASE) are
    properly controlled by the CLICKHOUSE_ALLOW_DROP flag when writes are enabled.
    """

    @classmethod
    def setUpClass(cls):
        """Set up the environment before tests."""
        cls.client = create_clickhouse_client()
        cls.test_db = "test_drop_protection_db"
        cls.test_table = "drop_test_table"

        # Use direct client commands for setup (bypassing run_query)
        cls.client.command(f"DROP DATABASE IF EXISTS {cls.test_db}")
        cls.client.command(f"CREATE DATABASE {cls.test_db}")
        cls.client.command(f"""
            CREATE TABLE {cls.test_db}.{cls.test_table} (
                id UInt32,
                value String
            ) ENGINE = MergeTree()
            ORDER BY id
        """)

    @classmethod
    def tearDownClass(cls):
        """Clean up the environment after tests."""
        cls.client.command(f"DROP DATABASE IF EXISTS {cls.test_db}")

    @patch.dict(os.environ, {"CLICKHOUSE_READ_ONLY": "false", "CLICKHOUSE_ALLOW_DROP": "false"})
    def test_drop_table_blocked_when_flag_not_set(self):
        """Test that DROP TABLE is blocked when CLICKHOUSE_ALLOW_DROP=false."""
        drop_query = f"DROP TABLE {self.test_db}.{self.test_table}"

        # Should raise ToolError due to DROP protection
        with self.assertRaises(ToolError) as context:
            run_query(drop_query)

        error_msg = str(context.exception)
        self.assertIn("DROP operations are not allowed", error_msg)
        self.assertIn("CLICKHOUSE_ALLOW_DROP=true", error_msg)

    @patch.dict(os.environ, {"CLICKHOUSE_READ_ONLY": "false", "CLICKHOUSE_ALLOW_DROP": "false"})
    def test_drop_database_blocked_when_flag_not_set(self):
        """Test that DROP DATABASE is blocked when CLICKHOUSE_ALLOW_DROP=false."""
        temp_db = "test_temp_drop_db"
        self.client.command(f"CREATE DATABASE IF NOT EXISTS {temp_db}")

        drop_query = f"DROP DATABASE {temp_db}"

        # Should raise ToolError due to DROP protection
        with self.assertRaises(ToolError) as context:
            run_query(drop_query)

        error_msg = str(context.exception)
        self.assertIn("DROP operations are not allowed", error_msg)
        self.assertIn("CLICKHOUSE_ALLOW_DROP=true", error_msg)

        self.client.command(f"DROP DATABASE IF EXISTS {temp_db}")

    def test_drop_allowed_when_flag_set(self):
        """Test that DROP works when CLICKHOUSE_ALLOW_DROP=true."""
        # This test runs with ALLOW_DROP=true from the class decorator
        temp_table = "temp_drop_table"
        self.client.command(f"""
            CREATE TABLE {self.test_db}.{temp_table} (
                id UInt32
            ) ENGINE = MergeTree()
            ORDER BY id
        """)

        # Should succeed
        drop_query = f"DROP TABLE {self.test_db}.{temp_table}"
        result = run_query(drop_query)
        self.assertIsInstance(result, dict)

    @patch.dict(os.environ, {"CLICKHOUSE_READ_ONLY": "false", "CLICKHOUSE_ALLOW_DROP": "false"})
    def test_insert_allowed_without_drop_flag(self):
        """Test that INSERT works even when CLICKHOUSE_ALLOW_DROP=false."""
        insert_query = f"""
            INSERT INTO {self.test_db}.{self.test_table} (id, value)
            VALUES (1, 'test_value')
        """
        result = run_query(insert_query)
        self.assertIsInstance(result, dict)

        select_query = f"SELECT * FROM {self.test_db}.{self.test_table}"
        result = run_query(select_query)
        self.assertGreaterEqual(len(result["rows"]), 1)

    @patch.dict(os.environ, {"CLICKHOUSE_READ_ONLY": "false", "CLICKHOUSE_ALLOW_DROP": "false"})
    def test_create_allowed_without_drop_flag(self):
        """Test that CREATE TABLE works even when CLICKHOUSE_ALLOW_DROP=false."""
        create_query = f"""
            CREATE TABLE {self.test_db}.create_test (
                id UInt32
            ) ENGINE = MergeTree()
            ORDER BY id
        """
        result = run_query(create_query)
        self.assertIsInstance(result, dict)

        tables = list_tables(self.test_db)
        table_names = [t["name"] for t in tables]
        self.assertIn("create_test", table_names)

        self.client.command(f"DROP TABLE {self.test_db}.create_test")


class TestClickhouseReadOnlyMode(unittest.TestCase):
    """Tests for read-only mode functionality (CLICKHOUSE_READ_ONLY=true, default).

    These tests verify that write operations are properly blocked when
    CLICKHOUSE_READ_ONLY is true (the default setting).
    """

    @classmethod
    def setUpClass(cls):
        """Set up the environment before tests."""
        cls.env_patcher = patch.dict(os.environ, {"CLICKHOUSE_READ_ONLY": "true"})
        cls.env_patcher.start()

        cls.client = create_clickhouse_client()
        cls.test_db = "test_readonly_db"
        cls.test_table = "readonly_test_table"

        cls.client.command(f"DROP DATABASE IF EXISTS {cls.test_db}")
        cls.client.command(f"CREATE DATABASE {cls.test_db}")
        cls.client.command(f"""
            CREATE TABLE {cls.test_db}.{cls.test_table} (
                id UInt32,
                value String
            ) ENGINE = MergeTree()
            ORDER BY id
        """)

    @classmethod
    def tearDownClass(cls):
        """Clean up the environment after tests."""
        cls.client.command(f"DROP DATABASE IF EXISTS {cls.test_db}")
        cls.env_patcher.stop()

    def test_insert_blocked_in_readonly_mode(self):
        """Test that INSERT queries fail when CLICKHOUSE_READ_ONLY=true."""
        insert_query = f"""
            INSERT INTO {self.test_db}.{self.test_table} (id, value)
            VALUES (1, 'should_fail')
        """

        with self.assertRaises(ToolError) as context:
            run_query(insert_query)

        error_msg = str(context.exception)
        self.assertIn("Query execution failed", error_msg)
        self.assertTrue(
            "readonly" in error_msg.lower() or "cannot execute" in error_msg.lower(),
            f"Expected readonly-related error, got: {error_msg}",
        )

    def test_create_table_blocked_in_readonly_mode(self):
        """Test that CREATE TABLE queries fail when CLICKHOUSE_READ_ONLY=true."""
        create_query = f"""
            CREATE TABLE {self.test_db}.should_not_exist (
                id UInt32
            ) ENGINE = MergeTree()
            ORDER BY id
        """

        with self.assertRaises(ToolError) as context:
            run_query(create_query)

        error_msg = str(context.exception)
        self.assertIn("Query execution failed", error_msg)
        self.assertTrue(
            "readonly" in error_msg.lower() or "cannot execute" in error_msg.lower(),
            f"Expected readonly-related error, got: {error_msg}",
        )

    def test_alter_table_blocked_in_readonly_mode(self):
        """Test that ALTER TABLE queries fail when CLICKHOUSE_READ_ONLY=true."""
        alter_query = f"""
            ALTER TABLE {self.test_db}.{self.test_table}
            ADD COLUMN new_column String
        """

        with self.assertRaises(ToolError) as context:
            run_query(alter_query)

        error_msg = str(context.exception)
        self.assertIn("Query execution failed", error_msg)
        self.assertTrue(
            "readonly" in error_msg.lower() or "cannot execute" in error_msg.lower(),
            f"Expected readonly-related error, got: {error_msg}",
        )

    def test_select_allowed_in_readonly_mode(self):
        """Test that SELECT queries work normally in read-only mode."""
        select_query = f"SELECT * FROM {self.test_db}.{self.test_table}"
        result = run_query(select_query)

        self.assertIsInstance(result, dict)
        self.assertIn("columns", result)
        self.assertIn("rows", result)


if __name__ == "__main__":
    unittest.main()
