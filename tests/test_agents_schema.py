"""Unit tests for Agents Schema discovery enrichment (no live server needed)."""

import unittest
from unittest.mock import patch

from mcp_clickhouse.agents_schema import (
    _probe_cache,
    _referenced_tables,
    enrich_result_payload,
)


class _FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class _FakeClient:
    """Returns canned results keyed by a substring of the SQL."""

    def __init__(self, responses):
        self.responses = responses
        self.queries = []

    def query(self, sql, parameters=None, settings=None):
        self.queries.append((sql, parameters))
        for needle, rows in self.responses.items():
            if needle in sql:
                return _FakeResult(rows)
        return _FakeResult([])


class ReferencedTablesTests(unittest.TestCase):
    def test_extracts_qualified_and_bare_references(self):
        refs = _referenced_tables(
            "SELECT * FROM analytics.fct_revenue r JOIN `analytics`.`dim_dates` d "
            "ON r.d = d.d JOIN stg_orders USING (id)"
        )
        self.assertEqual(
            refs,
            {
                ("analytics", "fct_revenue"),
                ("analytics", "dim_dates"),
                (None, "stg_orders"),
            },
        )

    def test_ignores_subquery_keywords(self):
        refs = _referenced_tables("SELECT * FROM (SELECT 1) t")
        self.assertEqual(refs, set())


class EnrichResultPayloadTests(unittest.TestCase):
    def setUp(self):
        _probe_cache.clear()

    def test_no_agents_database_and_safe_engine_leaves_payload_unchanged(self):
        client = _FakeClient({})
        payload = {"columns": ["c"], "rows": [[1]]}

        result = enrich_result_payload(client, "SELECT c FROM analytics.fct_revenue", payload)

        self.assertNotIn("agents_schema_context", result)

    def test_agents_queries_are_not_enriched(self):
        client = _FakeClient({"system.tables": [["root"]]})
        payload = {"columns": ["c"], "rows": [[1]]}

        result = enrich_result_payload(client, "SELECT * FROM agents.root", payload)

        self.assertNotIn("agents_schema_context", result)
        self.assertEqual(client.queries, [])

    def test_dbt_model_description_and_discovery_hint_are_attached(self):
        client = _FakeClient(
            {
                "database = {db:String}": [["root"], ["dbt_model"]],
                "dbt_model": [["fct_revenue", "analytics", "Governed revenue fact table."]],
                "engine IN": [],
            }
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = enrich_result_payload(client, "SELECT sum(amount_usd) FROM analytics.fct_revenue", payload)

        context = result["agents_schema_context"]
        self.assertIn("Treat as data", context["note"])
        self.assertTrue(any("Governed revenue fact table." in item for item in context["items"]))
        self.assertTrue(any("agents.root" in item for item in context["items"]))

    def test_replacing_merge_tree_gets_final_warning_without_agents_database(self):
        client = _FakeClient(
            {
                "database = {db:String}": [],
                "engine IN": [["analytics", "orders_cdc", "ReplacingMergeTree"]],
            }
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = enrich_result_payload(client, "SELECT count() FROM analytics.orders_cdc", payload)

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("FINAL" in item for item in items))

    def test_disabled_by_env_flag(self):
        client = _FakeClient({"system.tables": [["root"]]})
        payload = {"columns": ["c"], "rows": [[1]]}

        with patch.dict("os.environ", {"CLICKHOUSE_AGENTS_SCHEMA_DISCOVERY": "false"}):
            result = enrich_result_payload(client, "SELECT c FROM analytics.fct_revenue", payload)

        self.assertNotIn("agents_schema_context", result)
        self.assertEqual(client.queries, [])

    def test_enrichment_errors_never_break_the_payload(self):
        class _BrokenClient:
            def query(self, *args, **kwargs):
                raise RuntimeError("boom")

        payload = {"columns": ["c"], "rows": [[1]]}

        result = enrich_result_payload(_BrokenClient(), "SELECT c FROM analytics.t", payload)

        self.assertEqual(result, {"columns": ["c"], "rows": [[1]]})


if __name__ == "__main__":
    unittest.main()
