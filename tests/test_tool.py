import unittest
import json

from dotenv import load_dotenv
from fastmcp.exceptions import ToolError

from mcp_clickhouse import create_clickhouse_client, list_databases, list_tables, run_select_query
from mcp_clickhouse.mcp_server import save_memory, get_memories_titles, get_memory, get_all_memories, delete_memory

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

        # Set up memory table for memory tests
        cls.client.command(f"DROP TABLE IF EXISTS {cls.test_db}.user_memory")
        cls.client.command(f"""
            CREATE TABLE {cls.test_db}.user_memory (
                key String,
                value String,
                created_at DateTime DEFAULT now(),
                updated_at DateTime DEFAULT now()
            ) ENGINE = MergeTree()
            ORDER BY key
        """)

    @classmethod
    def tearDownClass(cls):
        """Clean up the environment after tests."""
        cls.client.command(f"DROP DATABASE IF EXISTS {cls.test_db}")

    def setUp(self):
        """Set up clean state before each memory test."""
        # Clear memory table before each test - need to delete from default database too
        try:
            self.client.command("TRUNCATE TABLE user_memory")
        except Exception:
            pass  # Table may not exist in default database
        try:
            self.client.command(f"TRUNCATE TABLE {self.test_db}.user_memory")
        except Exception:
            pass  # Table may not exist in test database

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

    def test_run_select_query_success(self):
        """Test running a SELECT query successfully."""
        query = f"SELECT * FROM {self.test_db}.{self.test_table}"
        result = run_select_query(query)
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0][0], 1)
        self.assertEqual(result["rows"][0][1], "Alice")

    def test_run_select_query_failure(self):
        """Test running a SELECT query with an error."""
        query = f"SELECT * FROM {self.test_db}.non_existent_table"

        # Should raise ToolError
        with self.assertRaises(ToolError) as context:
            run_select_query(query)

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

    def test_save_memory_success(self):
        """Test saving memory successfully."""
        result = save_memory("Test Key", "Test Value")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["key"], "Test Key")
        self.assertIn("saved successfully", result["message"])

    def test_save_memory_multiple_same_key(self):
        """Test saving multiple memories with the same key."""
        save_memory("Duplicate Key", "First Value")
        result = save_memory("Duplicate Key", "Second Value")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["key"], "Duplicate Key")

    def test_get_memories_titles_empty(self):
        """Test getting titles from empty memory table."""
        result = get_memories_titles()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["titles"], [])

    def test_get_memories_titles_with_data(self):
        """Test getting titles with data in memory table."""
        save_memory("Key 1", "Value 1")
        save_memory("Key 2", "Value 2")
        
        result = get_memories_titles()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 2)
        self.assertIn("Key 1", result["titles"])
        self.assertIn("Key 2", result["titles"])

    def test_get_memory_success(self):
        """Test retrieving an existing memory."""
        save_memory("Retrieve Key", "Retrieve Value")
        
        result = get_memory("Retrieve Key")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["key"], "Retrieve Key")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["memories"][0]["value"], "Retrieve Value")

    def test_get_memory_not_found(self):
        """Test retrieving a non-existent memory."""
        result = get_memory("Non-existent Key")
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["key"], "Non-existent Key")

    def test_get_memory_multiple_entries(self):
        """Test retrieving multiple memories with the same key."""
        save_memory("Multi Key", "First Value")
        save_memory("Multi Key", "Second Value")
        
        result = get_memory("Multi Key")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(result["memories"]), 2)

    def test_get_all_memories_empty(self):
        """Test getting all memories from empty table."""
        result = get_all_memories()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 0)

    def test_get_all_memories_with_data(self):
        """Test getting all memories with data."""
        save_memory("All Key 1", "All Value 1")
        save_memory("All Key 2", "All Value 2")
        
        result = get_all_memories()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(result["memories"]), 2)

    def test_delete_memory_success(self):
        """Test deleting an existing memory."""
        save_memory("Delete Key", "Delete Value")
        
        result = delete_memory("Delete Key")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["key"], "Delete Key")
        self.assertEqual(result["deleted_count"], 1)
        
        # Verify it's actually deleted
        get_result = get_memory("Delete Key")
        self.assertEqual(get_result["status"], "not_found")

    def test_delete_memory_not_found(self):
        """Test deleting a non-existent memory."""
        result = delete_memory("Non-existent Delete Key")
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["key"], "Non-existent Delete Key")

    def test_memory_workflow_integration(self):
        """Test complete memory workflow integration."""
        # Save memory
        save_result = save_memory("Workflow Key", "Workflow Value")
        self.assertEqual(save_result["status"], "success")
        
        # Check it appears in titles
        titles_result = get_memories_titles()
        self.assertIn("Workflow Key", titles_result["titles"])
        
        # Retrieve it
        get_result = get_memory("Workflow Key")
        self.assertEqual(get_result["status"], "success")
        self.assertEqual(get_result["memories"][0]["value"], "Workflow Value")
        
        # Update it (save again)
        update_result = save_memory("Workflow Key", "Updated Value")
        self.assertEqual(update_result["status"], "success")
        
        # Verify update
        get_updated = get_memory("Workflow Key")
        self.assertEqual(get_updated["count"], 2)  # Now has 2 entries
        
        # Delete it
        delete_result = delete_memory("Workflow Key")
        self.assertEqual(delete_result["status"], "success")
        self.assertEqual(delete_result["deleted_count"], 2)
        
        # Verify deletion
        final_get = get_memory("Workflow Key")
        self.assertEqual(final_get["status"], "not_found")


class TestMemoryFlag(unittest.TestCase):
    """Test CLICKHOUSE_MEMORY flag functionality."""
    
    def test_memory_flag_controls_tool_availability(self):
        """Test that CLICKHOUSE_MEMORY flag controls whether memory tools are available."""
        # This test documents the expected behavior but cannot directly test
        # the conditional registration since it happens at module import time
        # In practice, this would be tested by running the server with different
        # CLICKHOUSE_MEMORY flag values and checking available tools
        
        # For now, we just verify the memory functions exist and work when flag is enabled
        # (which it must be for this test to run)
        from mcp_clickhouse.mcp_server import save_memory, get_memories_titles, get_memory, get_all_memories, delete_memory
        
        # These should be callable functions when CLICKHOUSE_MEMORY=true
        self.assertTrue(callable(save_memory))
        self.assertTrue(callable(get_memories_titles))
        self.assertTrue(callable(get_memory))
        self.assertTrue(callable(get_all_memories))
        self.assertTrue(callable(delete_memory))


if __name__ == "__main__":
    unittest.main()
