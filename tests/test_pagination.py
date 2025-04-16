import unittest

from dotenv import load_dotenv

from mcp_clickhouse import create_clickhouse_client, list_tables
from mcp_clickhouse.mcp_server import (
    table_pagination_cache,
    create_page_token,
    fetch_table_names,
    fetch_table_metadata,
    get_paginated_tables,
)

load_dotenv()


class TestPagination(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Set up the environment before tests."""
        cls.client = create_clickhouse_client()

        # Prepare test database
        cls.test_db = "test_pagination_db"
        cls.client.command(f"CREATE DATABASE IF NOT EXISTS {cls.test_db}")

        # Create 10 test tables to test pagination
        for i in range(1, 11):
            table_name = f"test_table_{i}"
            # Drop table if exists to ensure clean state
            cls.client.command(f"DROP TABLE IF EXISTS {cls.test_db}.{table_name}")

            # Create table with comments
            cls.client.command(f"""
                CREATE TABLE {cls.test_db}.{table_name} (
                    id UInt32 COMMENT 'ID field {i}',
                    name String COMMENT 'Name field {i}'
                ) ENGINE = MergeTree()
                ORDER BY id
                COMMENT 'Test table {i} for pagination testing'
            """)
            cls.client.command(f"""
                INSERT INTO {cls.test_db}.{table_name} (id, name) VALUES ({i}, 'Test {i}')
            """)

    @classmethod
    def tearDownClass(cls):
        """Clean up the environment after tests."""
        cls.client.command(f"DROP DATABASE IF EXISTS {cls.test_db}")

    def test_list_tables_pagination(self):
        """Test that list_tables returns paginated results."""
        # Test with page_size 3, should get 3 tables and a next_page_token
        result = list_tables(self.test_db, page_size=3)
        self.assertIsInstance(result, dict)
        self.assertIn("tables", result)
        self.assertIn("next_page_token", result)
        self.assertEqual(len(result["tables"]), 3)
        self.assertIsNotNone(result["next_page_token"])

        # Get the next page using the token
        page_token = result["next_page_token"]
        result2 = list_tables(self.test_db, page_token=page_token, page_size=3)
        self.assertEqual(len(result2["tables"]), 3)
        self.assertIsNotNone(result2["next_page_token"])

        # The tables in the second page should be different from the first page
        page1_table_names = {table["name"] for table in result["tables"]}
        page2_table_names = {table["name"] for table in result2["tables"]}
        self.assertEqual(len(page1_table_names.intersection(page2_table_names)), 0)

        # Get the third page
        page_token = result2["next_page_token"]
        result3 = list_tables(self.test_db, page_token=page_token, page_size=3)
        self.assertEqual(len(result3["tables"]), 3)
        self.assertIsNotNone(result3["next_page_token"])

        # Get the fourth (last) page
        page_token = result3["next_page_token"]
        result4 = list_tables(self.test_db, page_token=page_token, page_size=3)
        self.assertEqual(len(result4["tables"]), 1)  # Only 1 table left
        self.assertIsNone(result4["next_page_token"])  # No more pages

    def test_invalid_page_token(self):
        """Test that list_tables handles invalid page tokens gracefully."""
        # Test with an invalid page token
        result = list_tables(self.test_db, page_token="invalid_token", page_size=3)
        self.assertIsInstance(result, dict)
        self.assertIn("tables", result)
        self.assertIn("next_page_token", result)
        self.assertEqual(len(result["tables"]), 3)  # Should return first page as fallback

    def test_token_for_different_database(self):
        """Test handling a token for a different database."""
        # Get first page and token for test_db
        result = list_tables(self.test_db, page_size=3)
        page_token = result["next_page_token"]

        # Try to use the token with a different database name
        # It should recognize the mismatch and fall back to first page

        # First, create another test database to use
        test_db2 = "test_pagination_db2"
        try:
            self.client.command(f"CREATE DATABASE IF NOT EXISTS {test_db2}")
            self.client.command(f"""
                CREATE TABLE {test_db2}.test_table (
                    id UInt32,
                    name String
                ) ENGINE = MergeTree()
                ORDER BY id
            """)

            # Use the token with a different database
            result2 = list_tables(test_db2, page_token=page_token, page_size=3)
            self.assertIsInstance(result2, dict)
            self.assertIn("tables", result2)
        finally:
            self.client.command(f"DROP DATABASE IF EXISTS {test_db2}")

    def test_different_page_sizes(self):
        """Test pagination with different page sizes."""
        # Get all tables in one page
        result = list_tables(self.test_db, page_size=20)
        self.assertEqual(len(result["tables"]), 10)  # All 10 tables
        self.assertIsNone(result["next_page_token"])  # No more pages

        # Get 5 tables per page
        result = list_tables(self.test_db, page_size=5)
        self.assertEqual(len(result["tables"]), 5)
        self.assertIsNotNone(result["next_page_token"])

        # Get second page with 5 tables
        page_token = result["next_page_token"]
        result2 = list_tables(self.test_db, page_token=page_token, page_size=5)
        self.assertEqual(len(result2["tables"]), 5)
        self.assertIsNone(result2["next_page_token"])  # No more pages

    def test_page_token_expiry(self):
        """Test that page tokens expire after their TTL."""
        # This test only works if we can modify the TTL for testing purposes
        # We'll set a very short TTL to test expiration

        # Get the first page with a token for the next page
        result = list_tables(self.test_db, page_size=3)
        page_token = result["next_page_token"]

        # Verify the token exists in the cache
        self.assertIn(page_token, table_pagination_cache)

        # For this test, we'll manually remove the token from the cache to simulate expiration
        # since we can't easily wait for the actual TTL (1 hour) to expire
        if page_token in table_pagination_cache:
            del table_pagination_cache[page_token]

        # Try to use the expired token
        result2 = list_tables(self.test_db, page_token=page_token, page_size=3)
        # Should fall back to first page
        self.assertEqual(len(result2["tables"]), 3)
        self.assertIsNotNone(result2["next_page_token"])

    def test_helper_functions(self):
        """Test the individual helper functions used for pagination."""
        client = create_clickhouse_client()

        # Test fetch_table_names
        table_names = fetch_table_names(client, self.test_db)
        self.assertEqual(len(table_names), 10)
        for i in range(1, 11):
            self.assertIn(f"test_table_{i}", table_names)

        # Test fetch_table_metadata
        sample_tables = table_names[:3]  # Get first 3 tables
        table_comments, column_comments = fetch_table_metadata(client, self.test_db, sample_tables)

        # Check table comments
        self.assertEqual(len(table_comments), 3)
        for table in sample_tables:
            self.assertIn(table, table_comments)
            self.assertIn("Test table", table_comments[table])

        # Check column comments
        self.assertEqual(len(column_comments), 3)
        for table in sample_tables:
            self.assertIn(table, column_comments)
            self.assertIn("id", column_comments[table])
            self.assertIn("name", column_comments[table])

        # Test get_paginated_tables
        tables, end_idx, has_more = get_paginated_tables(client, self.test_db, table_names, 0, 3)
        self.assertEqual(len(tables), 3)
        self.assertEqual(end_idx, 3)
        self.assertTrue(has_more)

        # Test create_page_token
        token = create_page_token(self.test_db, None, table_names, 3)
        self.assertIn(token, table_pagination_cache)
        cached_state = table_pagination_cache[token]
        self.assertEqual(cached_state["database"], self.test_db)
        self.assertEqual(cached_state["start_idx"], 3)
        self.assertEqual(cached_state["table_names"], table_names)


if __name__ == "__main__":
    unittest.main()
