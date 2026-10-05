"""Tests for Agents Schema discovery enrichment (no live server needed)."""

import asyncio
import concurrent.futures
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from mcp_clickhouse.agents_schema import (
    _CACHE_MAX_ENTRIES,
    _CACHE_TTL_SECONDS,
    _MAX_QUERY_CHARS,
    _MAX_REFERENCED_TABLES,
    _AgentsSchema,
    _context_query_settings,
    _referenced_tables,
    query_may_need_enrichment,
)
from mcp_clickhouse.mcp_env import MCPServerConfig
from mcp_clickhouse.mcp_server import _clickhouse_clients, _queries, mcp
from mcp_clickhouse.queries import _Queries, _retrieve_enrichment_result


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
                if isinstance(rows, Exception):
                    raise rows
                return _FakeResult(rows)
        return _FakeResult([])

    def get_client_setting(self, key):
        return None


@pytest.mark.parametrize("session_id", ["explicit-session", RuntimeError("unavailable")])
def test_session_ambiguity_skips_bare_names_but_preserves_qualified(monkeypatch, session_id):
    monkeypatch.setenv("CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY", "true")
    client = _FakeClient(
        {"engine LIKE": [["analytics", "orders", "ReplacingMergeTree"]]}, database="analytics"
    )

    def get_setting(key):
        if key == "session_id":
            if isinstance(session_id, Exception):
                raise session_id
            return session_id
        return None

    setting = MagicMock(side_effect=get_setting)
    monkeypatch.setattr(client, "get_client_setting", setting)
    owner = _AgentsSchema()
    # Even a warm current-database cache must not resolve session-local names.
    owner._remember(owner._current_db_cache, ("test",), "analytics")
    assert owner.enrich_result_payload(
        client, "SELECT * FROM orders", {"rows": [[7]]}, ("test",)
    ) == {"rows": [[7]]}
    assert client.queries == []
    result = owner.enrich_result_payload(
        client,
        "SELECT * FROM analytics.orders JOIN bare_table USING (id)",
        {"rows": [[1]]},
        ("test",),
    )
    assert any("ReplacingMergeTree" in item for item in result["agents_schema_context"]["items"])
    engine_query = next(params for sql, params in client.queries if "engine LIKE" in sql)
    assert engine_query["pairs"] == [("analytics", "orders")]
    assert not any("currentDatabase" in sql for sql, _ in client.queries)


@pytest.mark.parametrize("engine", ["ReplacingMergeTree", "CollapsingMergeTree"])
def test_engine_guidance_is_conditional_even_when_query_already_uses_final(monkeypatch, engine):
    monkeypatch.setenv("CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY", "true")
    client = _FakeClient({"engine LIKE": [["analytics", "orders", engine]]})
    payload = _AgentsSchema().enrich_result_payload(
        client, "SELECT count() FROM analytics.orders FINAL", {"rows": [[1]]}
    )
    note = payload["agents_schema_context"]["items"][0]
    assert "If the query does not already account for" in note
    assert "Add FINAL" not in note


@pytest.mark.parametrize(
    "server_value,readonly,client_value,expected",
    [
        (None, False, None, {"max_execution_time": 2}),
        ("0", False, None, {"max_execution_time": 2}),
        ("10", False, None, {"max_execution_time": 2}),
        ("1", False, None, {}),
        ("2", False, None, {}),
        ("0", True, None, {}),
        ("1", True, None, {}),
        ("10", True, None, {}),
        ("0", False, "0.5", {}),
        ("1", False, "0", {"max_execution_time": 2}),
        ("1", False, "10", {"max_execution_time": 2}),
    ],
)
def test_context_timeout_respects_effective_limits(server_value, readonly, client_value, expected):
    client = _FakeClient({})
    client.server_settings = (
        {"max_execution_time": SimpleNamespace(value=server_value, readonly=readonly)}
        if server_value is not None
        else {}
    )
    client.get_client_setting = MagicMock(return_value=client_value)
    assert _context_query_settings(client) == expected


@pytest.mark.parametrize(
    "query,expected",
    [
        ("SELECT 'FROM analytics.orders'", set()),
        ("SELECT 1 -- FROM analytics.orders", set()),
        ("SELECT 1 # JOIN analytics.orders", set()),
        ("SELECT /* FROM analytics.orders */ 1", set()),
        ("SELECT $sql$FROM analytics.orders$sql$", set()),
        ("WITH orders AS (SELECT 1) SELECT * FROM orders", set()),
        ("SELECT * FROM (WITH orders AS (SELECT 1) SELECT * FROM orders)", set()),
        ("SELECT * FROM remote('host', 'analytics', 'orders')", set()),
        ("SELECT * FROM analytics.orders(1)", set()),
        ("SELECT * FROM `analytics`.`orders-archive`", {("analytics", "orders-archive")}),
        ('SELECT * FROM "analytics" . "orders-archive"', {("analytics", "orders-archive")}),
        ("SELECT * FROM analytics /* comment */ . orders", {("analytics", "orders")}),
        ("SELECT extract(DAY FROM today())", set()),
        ("SELECT * FROM analytics.orders ARRAY JOIN items", {("analytics", "orders")}),
        ("SELECT * FROM analytics.orders\u00e9", set()),
        ("SELECT * FROM {table:Identifier}", set()),
        (
            "SELECT * FROM analytics.where JOIN analytics.orders",
            {("analytics", "where"), ("analytics", "orders")},
        ),
        ("SELECT * FROM (SELECT * FROM analytics.orders)", {("analytics", "orders")}),
        ("SELECT 'it\\'s FROM fake.orders' FROM analytics.orders", {("analytics", "orders")}),
        ("SELECT 'it''s FROM fake.orders' FROM analytics.orders", {("analytics", "orders")}),
        ("SELECT * FROM `db``name`.`orders`", {("db`name", "orders")}),
        ("SELECT * FROM `db\\name`.`orders`", set()),
        ("SELECT /* nested /* FROM fake.orders */ comment */ 1", set()),
        ("SELECT 'unterminated FROM fake.orders", set()),
        ("SELECT * FROM db.table.more", set()),
    ],
)
def test_reference_extraction_does_not_guess(query, expected):
    assert _referenced_tables(query) == expected


def test_reference_extraction_has_work_and_reference_bounds():
    query = "SELECT * FROM analytics.orders"
    assert not query_may_need_enrichment(query + " " * _MAX_QUERY_CHARS)
    assert not query_may_need_enrichment(
        "SELECT * FROM " + " JOIN ".join(f"db.t{i}" for i in range(_MAX_REFERENCED_TABLES + 1))
    )


@pytest.mark.parametrize("scope", [None, [], ("complete-config",)])
def test_all_metadata_caches_require_a_stable_scope_and_expire(monkeypatch, scope):
    monkeypatch.setenv("CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY", "true")
    clock = [100.0]
    monkeypatch.setattr(
        "mcp_clickhouse.agents_schema.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    owner = _AgentsSchema()
    client = _FakeClient({"currentDatabase": [["analytics"]]})
    for _ in range(2):
        owner.enrich_result_payload(client, "SELECT * FROM orders", {"rows": []}, scope)
    assert len(client.queries) == (3 if scope else 6)
    for cache in (owner._probe_cache, owner._engine_cache, owner._current_db_cache):
        assert bool(cache) is bool(scope)
    clock[0] += _CACHE_TTL_SECONDS + 1
    owner.enrich_result_payload(client, "SELECT * FROM orders", {"rows": []}, scope)
    assert len(client.queries) == (6 if scope else 9)


def test_metadata_cache_is_owned_by_each_query_handler(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY", "true")
    first = _Queries(MagicMock(), MagicMock())
    second = _Queries(MagicMock(), MagicMock())
    assert first.agents_schema is not second.agents_schema
    first.clients._acquire_clickhouse_client.return_value = SimpleNamespace(
        client=_FakeClient({"database = {db:String}": [["ROOT"]]})
    )
    second_client = _FakeClient({})
    second.clients._acquire_clickhouse_client.return_value = SimpleNamespace(client=second_client)
    args = ('{"rows":[]}', "SELECT * FROM analytics.orders", {"host": "same-host"})
    assert "agents_schema_context" in json.loads(first._enrichment_job(*args))
    assert "agents_schema_context" not in json.loads(second._enrichment_job(*args))
    assert len(second_client.queries) == 2


def test_uncacheable_requests_do_not_reuse_metadata(monkeypatch):
    class UncacheablePool:
        __hash__ = None

    monkeypatch.setenv("CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY", "true")
    # Deterministically model Python reusing an ID after a client is released.
    monkeypatch.setattr("mcp_clickhouse.queries.id", lambda _: 123, raising=False)
    clients = MagicMock()
    clients._acquire_clickhouse_client.side_effect = [
        SimpleNamespace(client=_FakeClient({"database = {db:String}": [["ROOT"]]})),
        SimpleNamespace(client=_FakeClient({})),
    ]
    queries = _Queries(MagicMock(), clients)
    results = [
        json.loads(
            queries._enrichment_job(
                '{"rows":[]}',
                "SELECT * FROM analytics.orders",
                {"username": user, "pool_mgr": UncacheablePool()},
            )
        )
        for user in ("first_account", "second_account")
    ]
    assert "agents_schema_context" in results[0]
    assert "agents_schema_context" not in results[1]
    assert clients._release_client_entry.call_count == 2
    assert not queries.agents_schema._probe_cache
    assert not queries.agents_schema._engine_cache
    assert not queries.agents_schema._current_db_cache


@pytest.mark.asyncio
async def test_stalled_enrichment_does_not_build_a_queue(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY", "true")
    monkeypatch.setattr("mcp_clickhouse.queries._ENRICHMENT_WAIT_SECONDS", 0.02)
    release = threading.Event()
    both_started = threading.Barrier(3)
    serialized = '{"rows":[[1]]}'

    def stalled(*args):
        both_started.wait(timeout=5)
        release.wait(timeout=5)
        return serialized

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        queries = _Queries(SimpleNamespace(enrichment=pool), MagicMock())
        monkeypatch.setattr(queries, "_enrichment_job", stalled)
        calls = [
            asyncio.create_task(
                queries._enrich_serialized_result_async(
                    serialized,
                    "SELECT * FROM analytics.orders",
                    {},
                )
            )
            for _ in range(2)
        ]
        try:
            await asyncio.to_thread(both_started.wait, 5)
            assert await asyncio.gather(*calls) == [serialized, serialized]
            # Both async and synchronous callers must skip rather than queue.
            for _ in range(20):
                assert (
                    await queries._enrich_serialized_result_async(
                        serialized,
                        "SELECT * FROM analytics.orders",
                        {},
                    )
                    == serialized
                )
                assert (
                    queries._enrich_serialized_result(
                        serialized,
                        "SELECT * FROM analytics.orders",
                        {},
                    )
                    == serialized
                )
            assert pool._work_queue.qsize() == 0
        finally:
            release.set()
            await asyncio.gather(*calls)
    # Completion, not caller timeout, makes both slots available again.
    assert queries._enrichment_slots.acquire(blocking=False)
    assert queries._enrichment_slots.acquire(blocking=False)
    assert not queries._enrichment_slots.acquire(blocking=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_call", [False, True])
async def test_failed_submission_releases_enrichment_capacity(monkeypatch, async_call):
    monkeypatch.setenv("CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY", "true")
    executors = MagicMock()
    executors.enrichment.submit.side_effect = RuntimeError("pool shut down")
    queries = _Queries(executors, MagicMock())
    for _ in range(4):
        args = ('{"rows":[]}', "SELECT * FROM analytics.orders", {})
        if async_call:
            result = await queries._enrich_serialized_result_async(*args)
        else:
            result = queries._enrich_serialized_result(*args)
        assert result == args[0]
    assert executors.enrichment.submit.call_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_reason", ["timeout", "cancel"])
async def test_late_failure_keeps_capacity_until_done_and_consumes_error(monkeypatch, exit_reason):
    monkeypatch.setenv("CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY", "true")
    monkeypatch.setattr(
        "mcp_clickhouse.queries._ENRICHMENT_WAIT_SECONDS", 0 if exit_reason == "timeout" else 5
    )
    future = concurrent.futures.Future()
    future.set_running_or_notify_cancel()
    executors = MagicMock()
    executors.enrichment.submit.return_value = future
    queries = _Queries(executors, MagicMock())
    with patch(
        "mcp_clickhouse.queries._retrieve_enrichment_result", wraps=_retrieve_enrichment_result
    ) as retrieve:
        task = asyncio.create_task(
            queries._enrich_serialized_result_async(
                '{"rows":[]}',
                "SELECT * FROM analytics.orders",
                {},
            )
        )
        await asyncio.sleep(0)
        if exit_reason == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task == '{"rows":[]}'
        assert not future.cancelled()
        assert queries._enrichment_slots.acquire(blocking=False)
        assert not queries._enrichment_slots.acquire(blocking=False)
        future.set_exception(RuntimeError("late connection failure"))
        for _ in range(3):
            await asyncio.sleep(0)
        retrieve.assert_called_once()
        assert queries._enrichment_slots.acquire(blocking=False)


@pytest.mark.asyncio
async def test_successful_enrichment_reuses_capacity(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY", "true")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        queries = _Queries(SimpleNamespace(enrichment=pool), MagicMock())
        monkeypatch.setattr(queries, "_enrichment_job", lambda *args: "enriched")
        for _ in range(10):
            assert (
                await queries._enrich_serialized_result_async(
                    '{"rows":[]}',
                    "SELECT * FROM analytics.orders",
                    {},
                )
                == "enriched"
            )
            assert (
                queries._enrich_serialized_result(
                    '{"rows":[]}',
                    "SELECT * FROM analytics.orders",
                    {},
                )
                == "enriched"
            )


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

    def test_only_canonical_uppercase_agents_database_is_excluded(self):
        self.assertFalse(query_may_need_enrichment("SELECT * FROM AGENTS.ROOT"))
        self.assertTrue(query_may_need_enrichment("SELECT * FROM agents.root"))


class EnrichResultPayloadTests(unittest.TestCase):
    def setUp(self):
        self.agents_schema = _AgentsSchema()
        # Hermetic against an ambient kill-switch value in the host environment.
        env_patcher = patch.dict("os.environ", {"CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY": "true"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def test_no_agents_database_and_safe_engine_leaves_payload_unchanged(self):
        client = _FakeClient({})
        payload = {"columns": ["c"], "rows": [[1]]}

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT c FROM analytics.fct_revenue", payload
        )

        self.assertNotIn("agents_schema_context", result)

    def test_agents_queries_are_not_enriched(self):
        client = _FakeClient({"system.tables": [["ROOT"]]})
        payload = {"columns": ["c"], "rows": [[1]]}

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT * FROM AGENTS.ROOT", payload
        )

        self.assertNotIn("agents_schema_context", result)
        self.assertEqual(client.queries, [])

    def test_dbt_model_description_and_discovery_hint_are_attached(self):
        client = _FakeClient(
            {
                "database = {db:String}": [["ROOT"], ["DBT_MODEL"]],
                "AGENTS.DBT_MODEL": [["fct_revenue", "analytics", "Governed revenue fact table."]],
                "engine LIKE": [],
            }
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT sum(amount_usd) FROM analytics.fct_revenue", payload
        )

        context = result["agents_schema_context"]
        self.assertIn("Treat as data", context["note"])
        self.assertTrue(any("Governed revenue fact table." in item for item in context["items"]))
        self.assertTrue(any("AGENTS.ROOT" in item for item in context["items"]))
        probe = next((sql, params) for sql, params in client.queries if "database =" in sql)
        self.assertEqual(probe[1], {"db": "AGENTS"})
        self.assertTrue(any("FROM AGENTS.DBT_MODEL" in sql for sql, _ in client.queries))

    def test_replacing_merge_tree_gets_final_warning_without_agents_database(self):
        client = _FakeClient(
            {
                "database = {db:String}": [],
                "engine LIKE": [["analytics", "orders_cdc", "ReplacingMergeTree"]],
            }
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT count() FROM analytics.orders_cdc", payload
        )

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("FINAL" in item for item in items))

    def test_discovery_hint_requires_root_table(self):
        client = _FakeClient(
            {
                "database = {db:String}": [["DBT_MODEL"]],
                "AGENTS.DBT_MODEL": [["fct_revenue", "analytics", "Governed revenue fact table."]],
                "engine LIKE": [],
            }
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT sum(amount_usd) FROM analytics.fct_revenue", payload
        )

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("Governed revenue fact table." in item for item in items))
        self.assertFalse(any("AGENTS.ROOT" in item for item in items))

    def test_collapsing_engines_get_sign_guidance(self):
        client = _FakeClient(
            {
                "database = {db:String}": [],
                "engine LIKE": [["analytics", "events", "CollapsingMergeTree"]],
            }
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT count() FROM analytics.events", payload
        )

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("sign column" in item for item in items))
        self.assertFalse(any("argMax" in item for item in items))

    def test_probe_cache_is_scoped_by_complete_client_config(self):
        client = _FakeClient({"database = {db:String}": [["ROOT"]]})
        readonly = (("settings", ("mapping", (("role", ("value", "reader")),))),)
        privileged = (("settings", ("mapping", (("role", ("value", "analyst")),))),)

        self.agents_schema.enrich_result_payload(
            client, "SELECT 1 FROM analytics.t", {"rows": []}, cache_scope=readonly
        )
        self.agents_schema.enrich_result_payload(
            client, "SELECT 1 FROM analytics.t", {"rows": []}, cache_scope=privileged
        )

        probe_queries = [q for q, _ in client.queries if "database =" in q]
        self.assertEqual(len(probe_queries), 2)

    def test_engine_warnings_and_hint_survive_dbt_note_cap(self):
        # Five dbt descriptions alone would fill MAX_CONTEXT_ITEMS; correctness
        # notes and the discovery hint must not be truncated away by them.
        dbt_rows = [[f"model_{i}", "analytics", f"Description {i}."] for i in range(5)]
        client = _FakeClient(
            {
                "database = {db:String}": [["ROOT"], ["DBT_MODEL"]],
                "AGENTS.DBT_MODEL": dbt_rows,
                "engine LIKE": [["analytics", "model_0", "ReplacingMergeTree"]],
            }
        )
        query = "SELECT 1 FROM " + " JOIN ".join(f"analytics.model_{i}" for i in range(5))

        result = self.agents_schema.enrich_result_payload(client, query, {"rows": []})

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("FINAL" in item for item in items))
        self.assertTrue(any("AGENTS.ROOT" in item for item in items))

    def test_dbt_lookup_failure_does_not_suppress_engine_warning_or_hint(self):
        client = _FakeClient(
            {
                "database = {db:String}": [["ROOT"], ["DBT_MODEL"]],
                "AGENTS.DBT_MODEL": RuntimeError("missing SELECT grant"),
                "engine LIKE": [["analytics", "orders", "ReplacingMergeTree"]],
            }
        )

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT count() FROM analytics.orders", {"rows": []}
        )

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("FINAL" in item for item in items))
        self.assertTrue(any("AGENTS.ROOT" in item for item in items))

    def test_agents_probe_failure_does_not_suppress_engine_warning(self):
        client = _FakeClient(
            {
                "database = {db:String}": RuntimeError("system.tables denied"),
                "engine LIKE": [["analytics", "orders", "ReplacingMergeTree"]],
            }
        )

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT count() FROM analytics.orders", {"rows": []}
        )

        self.assertTrue(any("FINAL" in item for item in result["agents_schema_context"]["items"]))

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

        with_bare = self.agents_schema.enrich_result_payload(
            client, "SELECT 1 FROM a.t JOIN b.t JOIN t", {"rows": []}, cache_scope=("test",)
        )
        without_bare = self.agents_schema.enrich_result_payload(
            client, "SELECT 1 FROM a.t JOIN b.t", {"rows": []}, cache_scope=("test",)
        )

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

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT count() FROM analytics.orders_cdc", payload
        )

        items = result["agents_schema_context"]["items"]
        self.assertTrue(
            any("SharedReplacingMergeTree" in item and "FINAL" in item for item in items)
        )

    def test_unqualified_reference_only_matches_current_database(self):
        client = _FakeClient(
            {
                "database = {db:String}": [],
                "engine LIKE": [["other_tenant", "orders", "ReplacingMergeTree"]],
            },
            database="default",
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT count() FROM orders", payload
        )

        self.assertNotIn("agents_schema_context", result)

    def test_engine_lookup_is_scoped_to_exact_references(self):
        client = _FakeClient({"database = {db:String}": [], "engine LIKE": []}, database="mydb")
        payload = {"columns": ["c"], "rows": [[1]]}

        self.agents_schema.enrich_result_payload(
            client, "SELECT c FROM analytics.fct_revenue JOIN bare_table", payload
        )

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
        exact = self.agents_schema.enrich_result_payload(
            _FakeClient(responses, database="default"), "SELECT 1 FROM CaseReview3.t", {"rows": []}
        )
        folded = self.agents_schema.enrich_result_payload(
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

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT count() FROM orders", {"rows": []}
        )

        items = result["agents_schema_context"]["items"]
        self.assertTrue(any("`analytics`.`orders`" in item for item in items))

    def test_failed_current_database_lookup_does_not_guess_default(self):
        client = _FakeClient(
            {
                "currentDatabase": RuntimeError("lookup timed out"),
                "database = {db:String}": [["ROOT"], ["DBT_MODEL"]],
                "AGENTS.DBT_MODEL": [["orders", "default", "Unrelated model."]],
                "engine LIKE": [["default", "orders", "ReplacingMergeTree"]],
            }
        )
        payload = {"columns": ["c"], "rows": [[1]]}

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT count() FROM orders", payload
        )

        self.assertEqual(result, {"columns": ["c"], "rows": [[1]]})
        self.assertEqual(len(client.queries), 1)
        self.assertFalse(self.agents_schema._current_db_cache)

    def test_failed_current_database_lookup_preserves_qualified_references(self):
        client = _FakeClient(
            {
                "currentDatabase": RuntimeError("lookup timed out"),
                "engine LIKE": [["analytics", "orders", "ReplacingMergeTree"]],
            }
        )

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT 1 FROM analytics.orders JOIN bare_table USING (id)", {"rows": []}
        )

        self.assertTrue(any("FINAL" in item for item in result["agents_schema_context"]["items"]))
        engine_queries = [(sql, params) for sql, params in client.queries if "engine LIKE" in sql]
        self.assertEqual(engine_queries[0][1]["pairs"], [("analytics", "orders")])
        self.assertEqual(sum("currentDatabase" in sql for sql, _ in client.queries), 1)

    def test_qualified_references_do_not_need_current_database_lookup(self):
        client = _FakeClient({"engine LIKE": [["analytics", "orders", "ReplacingMergeTree"]]})

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT count() FROM analytics.orders", {"rows": []}
        )

        self.assertTrue(any("FINAL" in item for item in result["agents_schema_context"]["items"]))
        self.assertFalse(any("currentDatabase" in sql for sql, _ in client.queries))

    def test_unqualified_agents_queries_are_not_enriched(self):
        client = _FakeClient({"database = {db:String}": [["ROOT"]]}, database="AGENTS")

        result = self.agents_schema.enrich_result_payload(
            client, "SELECT * FROM ROOT", {"rows": []}
        )

        self.assertEqual(result, {"rows": []})
        self.assertEqual(client.queries, [])

    def test_disabled_by_env_flag(self):
        client = _FakeClient({"system.tables": [["ROOT"]]})
        payload = {"columns": ["c"], "rows": [[1]]}

        with patch.dict("os.environ", {"CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY": "false"}):
            result = self.agents_schema.enrich_result_payload(
                client, "SELECT c FROM analytics.fct_revenue", payload
            )

        self.assertNotIn("agents_schema_context", result)
        self.assertEqual(client.queries, [])

    def test_probe_cache_stays_bounded(self):
        client = _FakeClient({"database = {db:String}": []})
        for i in range(_CACHE_MAX_ENTRIES):
            self.agents_schema._probe_cache[f"stale-key-{i}"] = (0.0, frozenset())

        self.agents_schema.enrich_result_payload(
            client,
            "SELECT c FROM analytics.fct_revenue",
            payload={"rows": []},
            cache_scope=("test",),
        )

        self.assertLessEqual(len(self.agents_schema._probe_cache), 1)

    def test_engine_notes_are_cached_per_client_and_tables(self):
        client = _FakeClient(
            {
                "database = {db:String}": [],
                "engine LIKE": [["analytics", "orders_cdc", "ReplacingMergeTree"]],
            }
        )

        self.agents_schema.enrich_result_payload(
            client, "SELECT 1 FROM analytics.orders_cdc", {"rows": []}, cache_scope=("test",)
        )
        self.agents_schema.enrich_result_payload(
            client, "SELECT 2 FROM analytics.orders_cdc", {"rows": []}, cache_scope=("test",)
        )

        engine_queries = [sql for sql, _ in client.queries if "engine LIKE" in sql]
        self.assertEqual(len(engine_queries), 1)

    def test_enrichment_errors_never_break_the_payload(self):
        class _BrokenClient:
            def query(self, *args, **kwargs):
                raise RuntimeError("boom")

        payload = {"columns": ["c"], "rows": [[1]]}

        result = self.agents_schema.enrich_result_payload(
            _BrokenClient(), "SELECT c FROM analytics.t", payload
        )

        self.assertEqual(result, {"columns": ["c"], "rows": [[1]]})


class AgentsSchemaDiscoveryConfigTests(unittest.TestCase):
    def test_defaults_to_disabled(self):
        with patch.dict("os.environ"):
            os.environ.pop("CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY", None)
            self.assertFalse(MCPServerConfig().agents_schema_discovery)

    def test_parses_false(self):
        with patch.dict("os.environ", {"CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY": "False"}):
            self.assertFalse(MCPServerConfig().agents_schema_discovery)

    def test_parses_true(self):
        with patch.dict("os.environ", {"CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY": "true"}):
            self.assertTrue(MCPServerConfig().agents_schema_discovery)


class QueryEnrichmentExecutionTests(unittest.IsolatedAsyncioTestCase):
    def test_sync_timeout_returns_base_result_and_retains_job_slot(self):
        executors = MagicMock()
        clients = MagicMock()
        future = concurrent.futures.Future()
        executors.enrichment.submit.return_value = future
        queries = _Queries(executors, clients)
        serialized = '{"columns":["n"],"rows":[[1]]}'

        with (
            patch("mcp_clickhouse.queries.discovery_enabled", return_value=True),
            patch("mcp_clickhouse.queries.query_may_need_enrichment", return_value=True),
            patch("mcp_clickhouse.queries._ENRICHMENT_WAIT_SECONDS", 0),
        ):
            result = queries._enrich_serialized_result(serialized, "SELECT 1 FROM db.t", {})

        self.assertEqual(result, serialized)
        self.assertFalse(future.cancelled())
        self.assertTrue(queries._enrichment_slots.acquire(blocking=False))
        self.assertFalse(queries._enrichment_slots.acquire(blocking=False))
        future.set_result(serialized)
        self.assertTrue(queries._enrichment_slots.acquire(blocking=False))

    async def test_async_timeout_returns_base_result_and_retains_job_slot(self):
        executors = MagicMock()
        clients = MagicMock()
        future = concurrent.futures.Future()
        executors.enrichment.submit.return_value = future
        queries = _Queries(executors, clients)
        serialized = '{"columns":["n"],"rows":[[1]]}'

        with (
            patch("mcp_clickhouse.queries.discovery_enabled", return_value=True),
            patch("mcp_clickhouse.queries.query_may_need_enrichment", return_value=True),
            patch("mcp_clickhouse.queries._ENRICHMENT_WAIT_SECONDS", 0),
        ):
            result = await queries._enrich_serialized_result_async(
                serialized, "SELECT 1 FROM db.t", {}
            )

        self.assertEqual(result, serialized)
        self.assertFalse(future.cancelled())
        self.assertTrue(queries._enrichment_slots.acquire(blocking=False))
        self.assertFalse(queries._enrichment_slots.acquire(blocking=False))
        future.set_result(serialized)
        self.assertTrue(queries._enrichment_slots.acquire(blocking=False))


class _EnrichableFakeClient:
    """Fake client compatible with both execute_query and enrichment lookups."""

    server_version = "24.10"
    database = "default"

    def get_client_setting(self, key):
        return None

    def query(self, query, settings=None, parameters=None):
        if parameters and "db" in parameters:
            return SimpleNamespace(result_rows=[])
        if parameters and "pairs" in parameters:
            return SimpleNamespace(
                result_rows=[["analytics", "orders_cdc", "SharedReplacingMergeTree"]]
            )
        return SimpleNamespace(column_names=["c"], result_rows=[(1,)])


@pytest.mark.asyncio
async def test_run_query_tool_payload_includes_agents_schema_context(monkeypatch):
    """MCP boundary check: the registered tool returns the enriched JSON."""
    monkeypatch.setattr(_queries, "agents_schema", _AgentsSchema())
    _clickhouse_clients._clear_client_cache()
    env = {
        "CLICKHOUSE_HOST": "localhost",
        "CLICKHOUSE_USER": "default",
        "CLICKHOUSE_PASSWORD": "",
        "CLICKHOUSE_MCP_AGENTS_SCHEMA_DISCOVERY": "true",
    }
    try:
        with patch.dict("os.environ", env):
            with patch("mcp_clickhouse.clients.clickhouse_connect.get_client") as get_client:
                get_client.return_value = _EnrichableFakeClient()
                async with Client(mcp) as client:
                    result = await client.call_tool(
                        "run_query",
                        {
                            "query": ("SELECT c FROM analytics.orders_cdc WHERE id = {id:UInt32}"),
                            "params": {"id": 13},
                        },
                    )
        payload = json.loads(result.content[0].text)
        assert payload["rows"] == [[1]]
        context = payload["agents_schema_context"]
        assert any("SharedReplacingMergeTree" in item for item in context["items"])
    finally:
        _clickhouse_clients._clear_client_cache()


if __name__ == "__main__":
    unittest.main()
