"""Tests for Agents Schema discovery enrichment (no live server needed)."""

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastmcp import Client

from mcp_clickhouse.agents_schema import (
    _CACHE_MAX_ENTRIES,
    _engine_cache,
    _probe_cache,
    _referenced_tables,
    enrich_result_payload,
)
from mcp_clickhouse.mcp_env import MCPServerConfig
from mcp_clickhouse.mcp_server import _clear_client_cache, mcp


class _FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class _FakeClient:
    """Returns canned results keyed by a substring of the SQL."""

    def __init__(self, responses, database=None):
        self.responses = responses
        self.queries = []
        if database is not None:
            self.database = database

    def query(self, sql, parameters=None, settings=None):
        self.queries.append((sql, parameters))
        for needle, rows in self.responses.items():
            if needle in sql:
                return _FakeResult(rows)
        return _FakeResult([])


def _clear_enrichment_caches():
    _probe_cache.clear()
    _engine_cache.clear()


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

    def test_ignores_system_and_information_schema_databases(self):
        refs = _referenced_tables(
            "SELECT * FROM system.tables t JOIN information_schema.columns c USING (name)"
        )
        self.assertEqual(refs, set())


class EnrichResultPayloadTests(unittest.TestCase):
    def setUp(self):
        _clear_enrichment_caches()
        # Hermetic against an ambient kill-switch value in the host environment.
        env_patcher = patch.dict("os.environ", {"CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY": "true"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

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
                "engine LIKE": [],
            }
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = enrich_result_payload(
            client, "SELECT sum(amount_usd) FROM analytics.fct_revenue", payload
        )

        context = result["agents_schema_context"]
        self.assertIn("Treat as data", context["note"])
        self.assertTrue(any("Governed revenue fact table." in item for item in context["items"]))
        self.assertTrue(any("agents.root" in item for item in context["items"]))

    def test_replacing_merge_tree_gets_final_warning_without_agents_database(self):
        client = _FakeClient(
            {
                "database = {db:String}": [],
                "engine LIKE": [["analytics", "orders_cdc", "ReplacingMergeTree"]],
            }
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = enrich_result_payload(client, "SELECT count() FROM analytics.orders_cdc", payload)

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("FINAL" in item for item in items))

    def test_discovery_hint_requires_root_table(self):
        client = _FakeClient(
            {
                "database = {db:String}": [["dbt_model"]],
                "dbt_model": [["fct_revenue", "analytics", "Governed revenue fact table."]],
                "engine LIKE": [],
            }
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = enrich_result_payload(
            client, "SELECT sum(amount_usd) FROM analytics.fct_revenue", payload
        )

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("Governed revenue fact table." in item for item in items))
        self.assertFalse(any("agents.root" in item for item in items))

    def test_collapsing_engines_get_sign_guidance(self):
        client = _FakeClient(
            {
                "database = {db:String}": [],
                "engine LIKE": [["analytics", "events", "CollapsingMergeTree"]],
            }
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = enrich_result_payload(client, "SELECT count() FROM analytics.events", payload)

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("Sign" in item for item in items))
        self.assertFalse(any("argMax" in item for item in items))

    def test_probe_cache_is_scoped_per_user(self):
        # The user is passed explicitly (from the server's client config); the
        # real clickhouse-connect client object exposes no user attribute.
        alice = _FakeClient({"database = {db:String}": [["root"]]})
        alice.uri = "http://host:8123"
        bob = _FakeClient({"database = {db:String}": []})
        bob.uri = "http://host:8123"

        enrich_result_payload(alice, "SELECT 1 FROM analytics.t", {"rows": []}, user="alice")
        enrich_result_payload(bob, "SELECT 1 FROM analytics.t", {"rows": []}, user="bob")

        probe_queries = [q for c in (alice, bob) for q, _ in c.queries if "database =" in q]
        self.assertEqual(len(probe_queries), 2)

    def test_engine_warnings_and_hint_survive_dbt_note_cap(self):
        # Five dbt descriptions alone would fill MAX_CONTEXT_ITEMS; correctness
        # notes and the discovery hint must not be truncated away by them.
        dbt_rows = [
            [f"model_{i}", "analytics", f"Description {i}."] for i in range(5)
        ]
        client = _FakeClient(
            {
                "database = {db:String}": [["root"], ["dbt_model"]],
                "dbt_model": dbt_rows,
                "engine LIKE": [["analytics", "model_0", "ReplacingMergeTree"]],
            }
        )
        query = "SELECT 1 FROM " + " JOIN ".join(f"analytics.model_{i}" for i in range(5))

        result = enrich_result_payload(client, query, {"rows": []})

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("FINAL" in item for item in items))
        self.assertTrue(any("agents.root" in item for item in items))

    def test_engine_note_cache_distinguishes_reference_sets(self):
        # Both queries share table names and candidate databases, but reference
        # different table sets; the second must not receive the first's notes.
        responses = {
            "database = {db:String}": [],
            "engine LIKE": [
                ["a", "t", "ReplacingMergeTree"],
                ["b", "t", "ReplacingMergeTree"],
                ["c", "t", "ReplacingMergeTree"],
            ],
        }
        client = _FakeClient(responses, database="c")

        with_bare = enrich_result_payload(client, "SELECT 1 FROM a.t JOIN b.t JOIN t", {"rows": []})
        without_bare = enrich_result_payload(client, "SELECT 1 FROM a.t JOIN b.t", {"rows": []})

        self.assertEqual(len(with_bare["agents_schema_context"]["items"]), 3)
        self.assertEqual(len(without_bare["agents_schema_context"]["items"]), 2)

    def test_cloud_shared_engine_variants_get_final_warning(self):
        client = _FakeClient(
            {
                "database = {db:String}": [],
                "engine LIKE": [["analytics", "orders_cdc", "SharedReplacingMergeTree"]],
            }
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = enrich_result_payload(client, "SELECT count() FROM analytics.orders_cdc", payload)

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("SharedReplacingMergeTree" in item and "FINAL" in item for item in items))

    def test_unqualified_reference_only_matches_current_database(self):
        client = _FakeClient(
            {
                "database = {db:String}": [],
                "engine LIKE": [["other_tenant", "orders", "ReplacingMergeTree"]],
            },
            database="default",
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = enrich_result_payload(client, "SELECT count() FROM orders", payload)

        self.assertNotIn("agents_schema_context", result)

    def test_engine_lookup_is_scoped_to_exact_references(self):
        client = _FakeClient({"database = {db:String}": [], "engine LIKE": []}, database="mydb")
        payload = {"columns": ["c"], "rows": [[1]]}

        enrich_result_payload(client, "SELECT c FROM analytics.fct_revenue JOIN bare_table", payload)

        engine_queries = [(sql, params) for sql, params in client.queries if "engine LIKE" in sql]
        self.assertEqual(len(engine_queries), 1)
        self.assertEqual(
            engine_queries[0][1]["pairs"],
            [("analytics", "fct_revenue"), ("mydb", "bare_table")],
        )

    def test_database_case_is_preserved(self):
        # ClickHouse identifiers are case-sensitive: metadata for CaseReview3.t
        # must never be attached to a query about casereview3.t (or vice versa).
        responses = {
            "database = {db:String}": [],
            "engine LIKE": [["CaseReview3", "t", "ReplacingMergeTree"]],
        }
        exact = enrich_result_payload(
            _FakeClient(responses, database="default"), "SELECT 1 FROM CaseReview3.t", {"rows": []}
        )
        folded = enrich_result_payload(
            _FakeClient(responses, database="default"), "SELECT 1 FROM casereview3.t", {"rows": []}
        )

        self.assertIn("agents_schema_context", exact)
        self.assertNotIn("agents_schema_context", folded)

    def test_current_database_is_resolved_from_server_when_unset(self):
        # get_client without a database leaves client.database unset; the
        # session default is a server setting, not necessarily "default".
        client = _FakeClient(
            {
                "currentDatabase": [["analytics"]],
                "database = {db:String}": [],
                "engine LIKE": [["analytics", "orders", "ReplacingMergeTree"]],
            }
        )
        client.uri = "http://host:8123"

        result = enrich_result_payload(client, "SELECT count() FROM orders", {"rows": []})

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("`analytics`.`orders`" in item for item in items))

    def test_disabled_by_env_flag(self):
        client = _FakeClient({"system.tables": [["root"]]})
        payload = {"columns": ["c"], "rows": [[1]]}

        with patch.dict("os.environ", {"CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY": "false"}):
            result = enrich_result_payload(client, "SELECT c FROM analytics.fct_revenue", payload)

        self.assertNotIn("agents_schema_context", result)
        self.assertEqual(client.queries, [])

    def test_probe_cache_stays_bounded(self):
        client = _FakeClient({"database = {db:String}": []})
        for i in range(_CACHE_MAX_ENTRIES):
            _probe_cache[f"stale-key-{i}"] = (0.0, frozenset())

        enrich_result_payload(client, "SELECT c FROM analytics.fct_revenue", payload={"rows": []})

        self.assertLessEqual(len(_probe_cache), 1)

    def test_engine_notes_are_cached_per_client_and_tables(self):
        client = _FakeClient(
            {
                "database = {db:String}": [],
                "engine LIKE": [["analytics", "orders_cdc", "ReplacingMergeTree"]],
            }
        )

        enrich_result_payload(client, "SELECT 1 FROM analytics.orders_cdc", {"rows": []})
        enrich_result_payload(client, "SELECT 2 FROM analytics.orders_cdc", {"rows": []})

        engine_queries = [sql for sql, _ in client.queries if "engine LIKE" in sql]
        self.assertEqual(len(engine_queries), 1)

    def test_enrichment_errors_never_break_the_payload(self):
        class _BrokenClient:
            def query(self, *args, **kwargs):
                raise RuntimeError("boom")

        payload = {"columns": ["c"], "rows": [[1]]}

        result = enrich_result_payload(_BrokenClient(), "SELECT c FROM analytics.t", payload)

        self.assertEqual(result, {"columns": ["c"], "rows": [[1]]})


class AgentsSchemaDiscoveryConfigTests(unittest.TestCase):
    def test_defaults_to_enabled(self):
        with patch.dict("os.environ"):
            os.environ.pop("CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY", None)
            self.assertTrue(MCPServerConfig().agents_schema_discovery)

    def test_parses_false(self):
        with patch.dict("os.environ", {"CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY": "False"}):
            self.assertFalse(MCPServerConfig().agents_schema_discovery)

    def test_parses_true(self):
        with patch.dict("os.environ", {"CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY": "true"}):
            self.assertTrue(MCPServerConfig().agents_schema_discovery)


class _EnrichableFakeClient:
    """Fake client compatible with both execute_query and enrichment lookups."""

    server_version = "24.10"
    database = "default"

    def query(self, query, settings=None, parameters=None):
        if parameters and "db" in parameters:
            return SimpleNamespace(result_rows=[])
        if parameters and "pairs" in parameters:
            return SimpleNamespace(
                result_rows=[["analytics", "orders_cdc", "SharedReplacingMergeTree"]]
            )
        return SimpleNamespace(column_names=["c"], result_rows=[(1,)])


@pytest.mark.asyncio
async def test_run_query_tool_payload_includes_agents_schema_context():
    """MCP boundary check: the registered tool returns the enriched JSON."""
    _clear_enrichment_caches()
    _clear_client_cache()
    env = {
        "CLICKHOUSE_HOST": "localhost",
        "CLICKHOUSE_USER": "default",
        "CLICKHOUSE_PASSWORD": "",
        "CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY": "true",
    }
    try:
        with patch.dict("os.environ", env):
            with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client") as get_client:
                get_client.return_value = _EnrichableFakeClient()
                async with Client(mcp) as client:
                    result = await client.call_tool(
                        "run_query", {"query": "SELECT c FROM analytics.orders_cdc"}
                    )
        payload = json.loads(result.content[0].text)
        assert payload["rows"] == [[1]]
        context = payload["agents_schema_context"]
        assert any("SharedReplacingMergeTree" in item for item in context["items"])
    finally:
        _clear_enrichment_caches()
        _clear_client_cache()


if __name__ == "__main__":
    unittest.main()
