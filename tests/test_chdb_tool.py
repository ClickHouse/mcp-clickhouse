import unittest

from dotenv import load_dotenv

from mcp_clickhouse import create_chdb_client, run_chdb_select_query

load_dotenv()


class TestChDBTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Set up the environment before chDB tests."""
        cls.client = create_chdb_client()

    def test_run_chdb_select_query_simple(self):
        """Test running a simple SELECT query in chDB."""
        query = "SELECT 1 as test_value"
        result = run_chdb_select_query(query)
        self.assertIsInstance(result, list)
        self.assertIn("test_value", str(result))

    def test_run_chdb_select_query_with_url_table_function(self):
        """Test running a SELECT query with url table function in chDB."""
        query = "SELECT COUNT(1) FROM url('https://datasets.clickhouse.com/hits_compatible/athena_partitioned/hits_0.parquet', 'Parquet')"
        result = run_chdb_select_query(query)
        print(result)
        self.assertIsInstance(result, list)
        self.assertIn("1000000", str(result))

    def test_run_chdb_select_query_failure(self):
        """Test running a SELECT query with an error in chDB."""
        query = "SELECT * FROM non_existent_table_chDB"
        result = run_chdb_select_query(query)
        print(result)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["status"], "error")
        self.assertIn("message", result)

    def test_run_chdb_select_query_empty_result(self):
        """Test running a SELECT query that returns empty result in chDB."""
        query = "SELECT 1 WHERE 1 = 0"
        result = run_chdb_select_query(query)
        print(result)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)


if __name__ == "__main__":
    unittest.main()
