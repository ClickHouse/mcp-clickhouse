import unittest

from dotenv import load_dotenv

from mcp_clickhouse import (
    create_clickhouse_client,
    create_page_token,
    fetch_table_names_from_system,
    get_paginated_table_data,
    list_tables,
    table_pagination_cache,
)
from mcp_clickhouse.mcp_server import Table

load_dotenv()


class TestPagination(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Set up the environment before tests."""
        cls.client = create_clickhouse_client()

        cls.test_db = "test_pagination_db"
        cls.client.command(f"CREATE DATABASE IF NOT EXISTS {cls.test_db}")

        for i in range(1, 11):
            table_name = f"test_table_{i}"
            cls.client.command(f"DROP TABLE IF EXISTS {cls.test_db}.{table_name}")

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
        result = list_tables(self.test_db, page_size=3)
        self.assertIsInstance(result, dict)
        self.assertIn("tables", result)
        self.assertIn("next_page_token", result)
        self.assertIn("total_tables", result)
        self.assertEqual(len(result["tables"]), 3)
        self.assertIsNotNone(result["next_page_token"])
        self.assertEqual(result["total_tables"], 10)

        page_token = result["next_page_token"]
        result2 = list_tables(self.test_db, page_token=page_token, page_size=3)
        self.assertEqual(len(result2["tables"]), 3)
        self.assertIsNotNone(result2["next_page_token"])

        page1_table_names = {table["name"] for table in result["tables"]}
        page2_table_names = {table["name"] for table in result2["tables"]}
        self.assertEqual(len(page1_table_names.intersection(page2_table_names)), 0)

        page_token = result2["next_page_token"]
        result3 = list_tables(self.test_db, page_token=page_token, page_size=3)
        self.assertEqual(len(result3["tables"]), 3)
        self.assertIsNotNone(result3["next_page_token"])

        page_token = result3["next_page_token"]
        result4 = list_tables(self.test_db, page_token=page_token, page_size=3)
        self.assertEqual(len(result4["tables"]), 1)
        self.assertIsNone(result4["next_page_token"])

    def test_invalid_page_token(self):
        """Test that list_tables handles invalid page tokens gracefully."""
        result = list_tables(self.test_db, page_token="invalid_token", page_size=3)
        self.assertIsInstance(result, dict)
        self.assertIn("tables", result)
        self.assertIn("next_page_token", result)
        self.assertEqual(len(result["tables"]), 3)

    def test_token_for_different_database(self):
        """Test handling a token for a different database."""
        result = list_tables(self.test_db, page_size=3)
        page_token = result["next_page_token"]
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

            result2 = list_tables(test_db2, page_token=page_token, page_size=3)
            self.assertIsInstance(result2, dict)
            self.assertIn("tables", result2)
        finally:
            self.client.command(f"DROP DATABASE IF EXISTS {test_db2}")

    def test_different_page_sizes(self):
        """Test pagination with different page sizes."""
        result = list_tables(self.test_db, page_size=20)
        self.assertEqual(len(result["tables"]), 10)
        self.assertIsNone(result["next_page_token"])

        result = list_tables(self.test_db, page_size=5)
        self.assertEqual(len(result["tables"]), 5)
        self.assertIsNotNone(result["next_page_token"])

        page_token = result["next_page_token"]
        result2 = list_tables(self.test_db, page_token=page_token, page_size=5)
        self.assertEqual(len(result2["tables"]), 5)
        self.assertIsNone(result2["next_page_token"])

    def test_page_token_expiry(self):
        """Test that page tokens expire after their TTL."""
        result = list_tables(self.test_db, page_size=3)
        page_token = result["next_page_token"]

        self.assertIn(page_token, table_pagination_cache)

        # For this test manually remove the token from the cache to simulate expiration
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

        table_names = fetch_table_names_from_system(client, self.test_db)
        self.assertEqual(len(table_names), 10)
        for i in range(1, 11):
            self.assertIn(f"test_table_{i}", table_names)

        tables, end_idx, has_more = get_paginated_table_data(
            client, self.test_db, table_names, 0, 3
        )
        self.assertEqual(len(tables), 3)
        self.assertEqual(end_idx, 3)
        self.assertTrue(has_more)

        for table in tables:
            self.assertIsInstance(table, Table)
            self.assertEqual(table.database, self.test_db)
            self.assertIsInstance(table.columns, list)

        token = create_page_token(self.test_db, None, None, table_names, 3, True)
        self.assertIn(token, table_pagination_cache)
        cached_state = table_pagination_cache[token]
        self.assertEqual(cached_state["database"], self.test_db)
        self.assertEqual(cached_state["start_idx"], 3)
        self.assertEqual(cached_state["table_names"], table_names)
        self.assertEqual(cached_state["include_detailed_columns"], True)

    def test_filters_with_pagination(self):
        """Test pagination with LIKE and NOT LIKE filters."""
        result = list_tables(self.test_db, like="test_table_%", page_size=5)
        self.assertEqual(len(result["tables"]), 5)
        self.assertIsNotNone(result["next_page_token"])

        result2 = list_tables(
            self.test_db, like="test_table_%", page_token=result["next_page_token"], page_size=5
        )
        self.assertEqual(len(result2["tables"]), 5)
        self.assertIsNone(result2["next_page_token"])

        result3 = list_tables(self.test_db, not_like="test_table_1%", page_size=10)
        self.assertEqual(len(result3["tables"]), 8)
        self.assertIsNone(result3["next_page_token"])

    def test_metadata_trimming(self):
        """Test that include_detailed_columns parameter works correctly."""
        result_with_columns = list_tables(self.test_db, page_size=3, include_detailed_columns=True)
        self.assertIsInstance(result_with_columns, dict)
        self.assertIn("tables", result_with_columns)

        tables_with_columns = result_with_columns["tables"]
        self.assertEqual(len(tables_with_columns), 3)

        for table in tables_with_columns:
            self.assertIn("columns", table)
            self.assertIsInstance(table["columns"], list)
            self.assertGreater(len(table["columns"]), 0)
            for col in table["columns"]:
                self.assertIn("name", col)
                self.assertIn("column_type", col)

        result_without_columns = list_tables(
            self.test_db, page_size=3, include_detailed_columns=False
        )
        self.assertIsInstance(result_without_columns, dict)
        self.assertIn("tables", result_without_columns)

        tables_without_columns = result_without_columns["tables"]
        self.assertEqual(len(tables_without_columns), 3)

        for table in tables_without_columns:
            self.assertIn("columns", table)
            self.assertIsInstance(table["columns"], list)
            self.assertEqual(len(table["columns"]), 0)
            self.assertIn("create_table_query", table)
            self.assertIsInstance(table["create_table_query"], str)
            self.assertGreater(len(table["create_table_query"]), 0)

    def test_metadata_trimming_with_pagination(self):
        """Test that metadata trimming works across multiple pages."""
        result1 = list_tables(self.test_db, page_size=3, include_detailed_columns=False)
        self.assertEqual(len(result1["tables"]), 3)
        self.assertIsNotNone(result1["next_page_token"])

        for table in result1["tables"]:
            self.assertEqual(len(table["columns"]), 0)

        result2 = list_tables(
            self.test_db,
            page_token=result1["next_page_token"],
            page_size=3,
            include_detailed_columns=False,
        )
        self.assertEqual(len(result2["tables"]), 3)

        for table in result2["tables"]:
            self.assertEqual(len(table["columns"]), 0)

    def test_metadata_setting_mismatch_resets_pagination(self):
        """Test that changing include_detailed_columns invalidates page token."""
        result1 = list_tables(self.test_db, page_size=3, include_detailed_columns=True)
        page_token = result1["next_page_token"]

        result2 = list_tables(
            self.test_db,
            page_token=page_token,
            page_size=3,
            include_detailed_columns=False,
        )

        self.assertEqual(len(result2["tables"]), 3)
        table_names_1 = [t["name"] for t in result1["tables"]]
        table_names_2 = [t["name"] for t in result2["tables"]]
        self.assertEqual(table_names_1, table_names_2)


if __name__ == "__main__":
    unittest.main()
