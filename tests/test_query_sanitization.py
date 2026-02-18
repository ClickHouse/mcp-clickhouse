import pytest
from fastmcp.exceptions import ToolError

from mcp_clickhouse.mcp_server import validate_query_is_readonly


class TestValidateQueryIsReadonly:
    """Tests for SQL query sanitization in run_select_query.

    These tests verify that validate_query_is_readonly correctly allows
    read-only queries and blocks destructive operations, providing
    defense-in-depth on top of the ClickHouse readonly setting.
    """

    # ── Allowed queries ──────────────────────────────────────────────

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1",
            "select 1",
            "  SELECT 1",
            "SELECT * FROM system.tables",
            "SELECT count() FROM db.my_table WHERE id > 10",
            "SHOW DATABASES",
            "SHOW TABLES FROM default",
            "show tables",
            "DESCRIBE TABLE db.my_table",
            "DESC TABLE db.my_table",
            "EXPLAIN SELECT 1",
            "EXISTS TABLE db.my_table",
            "WITH cte AS (SELECT 1) SELECT * FROM cte",
            "with x as (select 1) select * from x",
        ],
        ids=[
            "simple_select",
            "lowercase_select",
            "leading_whitespace",
            "select_from_table",
            "select_with_where",
            "show_databases",
            "show_tables",
            "lowercase_show",
            "describe_table",
            "desc_table",
            "explain_select",
            "exists_table",
            "cte_with_select",
            "lowercase_cte",
        ],
    )
    def test_allows_readonly_queries(self, query):
        # Should not raise
        validate_query_is_readonly(query)

    # ── Blocked: wrong statement type ────────────────────────────────

    @pytest.mark.parametrize(
        "query",
        [
            "INSERT INTO db.t VALUES (1, 'a')",
            "insert into db.t values (1, 'a')",
            "DELETE FROM db.t WHERE id = 1",
            "UPDATE db.t SET name = 'x' WHERE id = 1",
            "DROP TABLE db.t",
            "DROP DATABASE mydb",
            "ALTER TABLE db.t ADD COLUMN x UInt32",
            "CREATE TABLE db.t (id UInt32) ENGINE = MergeTree() ORDER BY id",
            "TRUNCATE TABLE db.t",
            "RENAME TABLE db.t TO db.t2",
            "ATTACH TABLE db.t",
            "DETACH TABLE db.t",
            "GRANT SELECT ON db.t TO user1",
            "REVOKE SELECT ON db.t FROM user1",
            "KILL QUERY WHERE query_id = 'abc'",
            "OPTIMIZE TABLE db.t",
            "SYSTEM RELOAD DICTIONARY dict1",
            "SET max_memory_usage = 1000000000",
            "EXCHANGE TABLES db.t AND db.t2",
        ],
        ids=[
            "insert",
            "insert_lowercase",
            "delete",
            "update",
            "drop_table",
            "drop_database",
            "alter_table",
            "create_table",
            "truncate",
            "rename",
            "attach",
            "detach",
            "grant",
            "revoke",
            "kill_query",
            "optimize",
            "system_reload",
            "set",
            "exchange",
        ],
    )
    def test_blocks_destructive_statements(self, query):
        with pytest.raises(ToolError):
            validate_query_is_readonly(query)

    # ── Blocked: multi-statement injection ───────────────────────────

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1; DROP TABLE db.t",
            "SELECT 1; INSERT INTO db.t VALUES (1)",
            "SELECT 1; DELETE FROM db.t",
            "SELECT 1;\nDROP TABLE db.t",
            "SELECT 1; TRUNCATE TABLE db.t",
            "SELECT 1; CREATE TABLE db.t2 (id UInt32) ENGINE=MergeTree() ORDER BY id",
        ],
        ids=[
            "select_then_drop",
            "select_then_insert",
            "select_then_delete",
            "select_then_drop_newline",
            "select_then_truncate",
            "select_then_create",
        ],
    )
    def test_blocks_multi_statement_injection(self, query):
        with pytest.raises(ToolError):
            validate_query_is_readonly(query)

    # ── Blocked: comment-based bypass attempts ───────────────────────

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1; /* harmless */ DROP TABLE db.t",
            "SELECT 1; -- comment\nDROP TABLE db.t",
            "/* SELECT */ DROP TABLE db.t",
        ],
        ids=[
            "block_comment_bypass",
            "line_comment_bypass",
            "comment_hiding_select",
        ],
    )
    def test_blocks_comment_injection(self, query):
        with pytest.raises(ToolError):
            validate_query_is_readonly(query)

    # ── Allowed: dangerous-looking words inside string literals ──────

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM db.t WHERE name = 'DROP TABLE'",
            "SELECT * FROM db.t WHERE status = 'DELETE'",
            "SELECT * FROM db.t WHERE msg = 'INSERT INTO something'",
            "SELECT 'CREATE TABLE' AS keyword",
        ],
        ids=[
            "drop_in_string",
            "delete_in_string",
            "insert_in_string",
            "create_in_string",
        ],
    )
    def test_allows_dangerous_words_in_string_literals(self, query):
        # Dangerous keywords inside single-quoted strings should be allowed
        validate_query_is_readonly(query)

    # ── Edge cases ───────────────────────────────────────────────────

    def test_empty_query_blocked(self):
        with pytest.raises(ToolError):
            validate_query_is_readonly("")

    def test_whitespace_only_blocked(self):
        with pytest.raises(ToolError):
            validate_query_is_readonly("   ")

    def test_comment_only_blocked(self):
        with pytest.raises(ToolError):
            validate_query_is_readonly("-- just a comment")

    def test_select_with_subquery(self):
        query = "SELECT * FROM (SELECT id, name FROM db.t) AS sub"
        validate_query_is_readonly(query)

    def test_select_with_union(self):
        query = "SELECT 1 UNION ALL SELECT 2"
        validate_query_is_readonly(query)
