"""Tests for the Postgres destructive-statement guard."""

import pytest
from fastmcp.exceptions import ToolError

from mcp_clickhouse.postgres_backend import (
    _validate_postgres_query_for_destructive_ops,
    _validate_postgres_query_for_transaction_control,
)


@pytest.fixture
def write_mode(monkeypatch):
    monkeypatch.setenv("POSTGRES_ALLOW_WRITE_ACCESS", "true")
    monkeypatch.delenv("POSTGRES_ALLOW_DROP", raising=False)


@pytest.mark.parametrize(
    "query",
    [
        "DROP TABLE t",
        "drop schema s cascade",
        "ALTER TABLE t DROP COLUMN c",
        "TRUNCATE t",
        "DELETE FROM t",
        "UPDATE t SET a = 1",
        "INSERT INTO t VALUES (1) ON CONFLICT (a) DO UPDATE SET a = 2",
        "CREATE OR REPLACE VIEW v AS SELECT 1",
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
        # # is an operator in Postgres, not a comment.
        "SELECT data #> '{a}' FROM t; DROP TABLE t",
        # Postgres nests block comments, so the guard stays conservative.
        "/* a /* b */ DROP */ SELECT 1",
        "SELECT 'a''b'; DROP TABLE t",
        "SELECT E'it\\'s'; DROP TABLE t",
        "SELECT $$x$$; DROP TABLE t",
        # Dollar-quoted bodies are checked, so DO blocks and functions cannot hide them.
        "DO $$ BEGIN IF EXISTS (SELECT 1) THEN DROP TABLE t; END IF; END $$",
        "CREATE FUNCTION f() RETURNS void AS $body$ DELETE FROM t $body$ LANGUAGE sql",
        "CREATE RULE r AS ON DELETE TO t DO INSTEAD DELETE FROM u",
    ],
)
def test_blocks_destructive_statements_in_write_mode(write_mode, query):
    with pytest.raises(ToolError, match="POSTGRES_ALLOW_DROP=true"):
        _validate_postgres_query_for_destructive_ops(query)


@pytest.mark.parametrize(
    "query",
    [
        "CREATE TABLE t (a int)",
        "INSERT INTO t VALUES (1)",
        "SELECT 'DROP TABLE t'",
        "SELECT E'\\' DROP TABLE t'",
        'SELECT 1 AS "update"',
        "SELECT 1 -- DROP TABLE t",
        "SELECT 1 /* DELETE FROM t */",
        "CREATE TABLE c (p int REFERENCES p (id) ON DELETE CASCADE ON UPDATE NO ACTION)",
        "CREATE TRIGGER g BEFORE INSERT OR UPDATE ON t FOR EACH ROW EXECUTE FUNCTION f()",
        "CREATE TRIGGER g AFTER DELETE ON t FOR EACH ROW EXECUTE FUNCTION f()",
        "CREATE TRIGGER g INSTEAD OF UPDATE ON v FOR EACH ROW EXECUTE FUNCTION f()",
        "GRANT SELECT, UPDATE, DELETE ON t TO app",
        "REVOKE UPDATE ON t FROM app",
        "SELECT $1",
        "SELECT dropped_at, updated_at FROM t",
        "SELECT replace(a, 'x', 'y') FROM t",
    ],
)
def test_allows_other_statements_in_write_mode(write_mode, query):
    _validate_postgres_query_for_destructive_ops(query)


def test_skipped_in_read_only_mode(monkeypatch):
    monkeypatch.delenv("POSTGRES_ALLOW_WRITE_ACCESS", raising=False)
    monkeypatch.delenv("POSTGRES_ALLOW_DROP", raising=False)

    _validate_postgres_query_for_destructive_ops("DROP TABLE t")


def test_allow_drop_permits_destructive_statements(write_mode, monkeypatch):
    monkeypatch.setenv("POSTGRES_ALLOW_DROP", "true")

    _validate_postgres_query_for_destructive_ops("DROP TABLE t")


@pytest.mark.parametrize(
    "query",
    [
        "COMMIT",
        "end",
        "ROLLBACK",
        "abort",
        "BEGIN",
        "START TRANSACTION READ WRITE",
        "PREPARE TRANSACTION 'x'",
        "  -- leading comment\n/* and block */ COMMIT",
    ],
)
def test_rejects_transaction_control_in_any_mode(query):
    with pytest.raises(ToolError, match="Transaction control statements"):
        _validate_postgres_query_for_transaction_control(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 'COMMIT'",
        'SELECT 1 AS "begin"',
        "PREPARE q AS SELECT 1",
        "SAVEPOINT s",
        "DO $$ BEGIN PERFORM 1; END $$",
        "SELECT ending FROM t",
    ],
)
def test_allows_other_statements_starting_with_similar_words(query):
    _validate_postgres_query_for_transaction_control(query)
