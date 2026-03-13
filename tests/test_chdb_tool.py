import os
import tempfile
import unittest
import importlib.util

from dotenv import load_dotenv

from mcp_clickhouse import mcp_server

load_dotenv()


@unittest.skipUnless(importlib.util.find_spec("chdb") is not None, "requires chdb extra")
class TestChDBTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Set up the environment before chDB tests."""
        os.environ["CHDB_ENABLED"] = "true"
        if mcp_server._chdb_client is None:
            mcp_server._chdb_client = mcp_server._init_chdb_client()
        cls.client = mcp_server.create_chdb_client()

    def test_run_chdb_select_query_simple(self):
        """Test running a simple SELECT query in chDB."""
        query = "SELECT 1 as test_value"
        result = mcp_server.run_chdb_select_query(query)
        self.assertIsInstance(result, list)
        self.assertIn("test_value", str(result))

    def test_run_chdb_select_query_with_file_table_function(self):
        """Test running a SELECT query against a local file via chDB."""
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as temp_file:
            temp_file.write("value\n1\n2\n3\n")
            temp_path = temp_file.name

        self.addCleanup(lambda: os.path.exists(temp_path) and os.unlink(temp_path))
        query = f"SELECT SUM(value) AS total FROM file('{temp_path}', 'CSVWithNames')"
        result = mcp_server.run_chdb_select_query(query)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["total"], 6)

    def test_run_chdb_select_query_failure(self):
        """Test running a SELECT query with an error in chDB."""
        query = "SELECT * FROM non_existent_table_chDB"
        result = mcp_server.run_chdb_select_query(query)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["status"], "error")
        self.assertIn("message", result)

    def test_run_chdb_select_query_empty_result(self):
        """Test running a SELECT query that returns empty result in chDB."""
        query = "SELECT 1 WHERE 1 = 0"
        result = mcp_server.run_chdb_select_query(query)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)


if __name__ == "__main__":
    unittest.main()
