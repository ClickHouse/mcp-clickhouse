import unittest

from dotenv import load_dotenv
from fastmcp.exceptions import ToolError
from mcp_clickhouse import list_chdb_tenants, create_chdb_client, run_chdb_select_query

load_dotenv()

class TestChDBTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Set up the environment before chDB tests."""
        cls.client = create_chdb_client(tenant="default")

    def test_list_chdb_tenants(self):
        tenants = list_chdb_tenants()
        self.assertIn("default", tenants)
        self.assertEqual(len(tenants), 1)

    def test_run_chdb_select_query_wrong_tenant(self):
        """Test running a simple SELECT query in chDB with wrong tenant."""
        tenant = "wrong_tenant"
        query = "SELECT 1 as test_value"
        with self.assertRaises(ToolError) as cm:
            run_chdb_select_query(tenant, query)

        self.assertIn(
            f"chDB query not performed for invalid tenant - '{tenant}'",
            str(cm.exception)
        )

    def test_run_chdb_select_query_simple(self):
        """Test running a simple SELECT query in chDB."""
        tenant = "default"
        query = "SELECT 1 as test_value"
        result = run_chdb_select_query(tenant, query)
        self.assertIsInstance(result, list)
        self.assertIn("test_value", str(result))

    def test_run_chdb_select_query_with_url_table_function(self):
        """Test running a SELECT query with url table function in chDB."""
        tenant = "default"
        query = "SELECT COUNT(1) FROM url('https://datasets.clickhouse.com/hits_compatible/athena_partitioned/hits_0.parquet', 'Parquet')"
        result = run_chdb_select_query(tenant, query)
        print(result)
        self.assertIsInstance(result, list)
        self.assertIn("1000000", str(result))

    def test_run_chdb_select_query_failure(self):
        """Test running a SELECT query with an error in chDB."""
        tenant = "default"
        query = "SELECT * FROM non_existent_table_chDB"
        result = run_chdb_select_query(tenant, query)
        print(result)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["status"], "error")
        self.assertIn("message", result)

    def test_run_chdb_select_query_empty_result(self):
        """Test running a SELECT query that returns empty result in chDB."""
        tenant = "default"
        query = "SELECT 1 WHERE 1 = 0"
        result = run_chdb_select_query(tenant, query)
        print(result)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)


if __name__ == "__main__":
    unittest.main()
