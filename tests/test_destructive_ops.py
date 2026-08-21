"""Tests for destructive-statement protection in the query validator.

These are unit tests for `_validate_query_for_destructive_ops` and its helper.
They do not need a running ClickHouse server. Integration coverage of the same
flag lives in `tests/test_tool.py`.
"""

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastmcp.exceptions import ToolError

from mcp_clickhouse.mcp_server import (
    _strip_comments_and_quoted_text,
    _validate_query_for_destructive_ops,
)


def _config(allow_write_access: bool = True, allow_drop: bool = False) -> SimpleNamespace:
    return SimpleNamespace(allow_write_access=allow_write_access, allow_drop=allow_drop)


@pytest.fixture
def write_mode_without_drop():
    """Write access enabled, destructive operations not opted in."""
    with patch("mcp_clickhouse.mcp_server.get_config", return_value=_config()):
        yield


# Every statement here deletes data or objects. All of them parse on ClickHouse.
DESTRUCTIVE_QUERIES = [
    "DROP TABLE d.t",
    "DROP TEMPORARY TABLE t",
    "DROP DATABASE d",
    "DROP VIEW d.v",
    "DROP DICTIONARY d.dict",
    "DROP FUNCTION f",
    "DROP NAMED COLLECTION nc",
    "DROP INDEX idx ON d.t",
    "DROP USER u",
    "DROP ROLE r",
    # The TABLE keyword is optional in ClickHouse's TRUNCATE grammar.
    "TRUNCATE d.t",
    "TRUNCATE TABLE d.t",
    "TRUNCATE IF EXISTS d.t",
    "TRUNCATE DATABASE d",
    "TRUNCATE ALL TABLES FROM d",
    # ALTER clauses that drop data or schema.
    "ALTER TABLE d.t DROP PARTITION tuple()",
    "ALTER TABLE d.t DROP PART 'all_1_1_0'",
    "ALTER TABLE d.t DROP COLUMN c",
    # DELETE and UPDATE, both the lightweight statements and the ALTER mutations.
    "DELETE FROM d.t WHERE 1",
    "ALTER TABLE d.t DELETE WHERE 1",
    "ALTER TABLE d.t UPDATE c = 0 WHERE 1",
    "UPDATE d.t SET c = 0 WHERE 1",
    # CLEAR COLUMN/INDEX/PROJECTION erase data even after the scrubber blanks
    # the partition literal.
    "ALTER TABLE d.t CLEAR COLUMN c IN PARTITION 'p'",
    "ALTER TABLE d.t CLEAR INDEX i IN PARTITION 'p'",
    "ALTER TABLE d.t CLEAR PROJECTION p IN PARTITION 'p'",
    # REPLACE forms overwrite existing data.
    "CREATE OR REPLACE TABLE d.t (a UInt8) ENGINE = MergeTree ORDER BY a",
    "REPLACE TABLE d.t (a UInt8) ENGINE = MergeTree ORDER BY a",
    "ALTER TABLE d.t REPLACE PARTITION 'p' FROM d.s",
    "CREATE OR REPLACE VIEW d.v AS SELECT 1",
    # DETACH is only destructive with PERMANENTLY.
    "DETACH TABLE d.t PERMANENTLY",
    "DETACH TABLE d.t SYNC PERMANENTLY",
    # Case and whitespace variations.
    "drop table d.t",
    "DROP\n  TABLE\n  d.t",
    "delete from d.t where 1",
    "Detach Table d.t Permanently",
    # Comments must not hide the statement.
    "-- harmless comment\nDROP TABLE d.t",
    "/* harmless comment */ DROP TABLE d.t",
    "# harmless comment\nTRUNCATE d.t",
    "DROP /* sneaky */ TABLE d.t",
]

# None of these delete anything, so write mode must still run them.
ALLOWED_QUERIES = [
    "SELECT 1",
    "SELECT * FROM d.t WHERE a = 1",
    "CREATE TABLE d.t (a UInt8) ENGINE = MergeTree ORDER BY a",
    "ALTER TABLE d.t ADD COLUMN b UInt8",
    "INSERT INTO d.t (a) VALUES (1)",
    # `truncate` is also a rounding function, `replace` a string function.
    "SELECT truncate(3.7)",
    "SELECT truncate(1.234, 2)",
    "INSERT INTO d.t SELECT truncate(a) FROM d.s",
    "SELECT replace(x, 'a', 'b') FROM d.t",
    "SELECT a OR replace(b, 'c', 'd') FROM d.t",
    # Keywords inside string literals and identifiers are data, not statements.
    "INSERT INTO d.logs (msg) VALUES ('drop the table')",
    "INSERT INTO d.logs (msg) VALUES ('truncate everything')",
    "SELECT * FROM d.logs WHERE msg = 'drop old table'",
    "SELECT * FROM d.logs WHERE msg = 'it''s a drop table day'",
    "SELECT * FROM d.logs WHERE msg = 'it\\'s a drop table day'",
    'SELECT "drop table" FROM d.t',
    "SELECT `drop table` FROM d.t",
    "SELECT $$drop table$$",
    "SELECT $tag$drop table$tag$",
    # Keywords inside comments are not statements either.
    "SELECT 1 -- drop table d.t",
    "SELECT 1 # drop table d.t",
    "SELECT 1 /* drop table d.t */",
    "INSERT INTO d.t SELECT 1 /* remember to\ndrop table d.old */",
    # Word boundaries.
    "SELECT dropped, undropped FROM d.t",
    "SELECT truncated FROM d.t",
    "SELECT updated_at, deleted_at FROM d.t",
    # Plain DETACH is reversible with ATTACH.
    "DETACH TABLE d.t",
    "ATTACH TABLE d.t",
    # New keywords inside literals and comments stay data.
    "SELECT * FROM d.t WHERE note = 'please delete this'",
    "SELECT 1 -- update d.t later",
]


@pytest.mark.parametrize("query", DESTRUCTIVE_QUERIES)
def test_destructive_queries_blocked(write_mode_without_drop, query):
    with pytest.raises(ToolError) as exc_info:
        _validate_query_for_destructive_ops(query)

    message = str(exc_info.value)
    assert "Destructive operations are not allowed" in message
    assert "CLICKHOUSE_ALLOW_DROP=true" in message
    assert "not a security boundary" in message


@pytest.mark.parametrize("query", ALLOWED_QUERIES)
def test_non_destructive_queries_allowed(write_mode_without_drop, query):
    _validate_query_for_destructive_ops(query)


@pytest.mark.parametrize("query", DESTRUCTIVE_QUERIES)
def test_destructive_queries_allowed_with_opt_in(query):
    config = _config(allow_write_access=True, allow_drop=True)
    with patch("mcp_clickhouse.mcp_server.get_config", return_value=config):
        _validate_query_for_destructive_ops(query)


@pytest.mark.parametrize("query", DESTRUCTIVE_QUERIES)
def test_validation_skipped_in_read_only_mode(query):
    """Read-only mode is enforced by the server-side readonly setting, not here."""
    config = _config(allow_write_access=False, allow_drop=False)
    with patch("mcp_clickhouse.mcp_server.get_config", return_value=config):
        _validate_query_for_destructive_ops(query)


@pytest.mark.parametrize(
    "query,expected_removed",
    [
        ("SELECT 'drop table'", "drop table"),
        ('SELECT "drop table"', "drop table"),
        ("SELECT `drop table`", "drop table"),
        ("SELECT $$drop table$$", "drop table"),
        ("SELECT $tag$drop table$tag$", "drop table"),
        ("SELECT 1 -- drop table", "drop table"),
        ("SELECT 1 # drop table", "drop table"),
        ("SELECT 1 /* drop table */", "drop table"),
    ],
)
def test_strip_comments_and_quoted_text(query, expected_removed):
    stripped = _strip_comments_and_quoted_text(query)

    assert expected_removed not in stripped
    assert stripped.startswith("SELECT")


def test_strip_keeps_tokens_separated():
    assert _strip_comments_and_quoted_text("DROP/* c */TABLE t") == "DROP TABLE t"


def test_strip_leaves_unterminated_literal_in_place():
    """An unterminated literal is a syntax error; keep the guard conservative."""
    query = "INSERT INTO t VALUES ('drop table"

    assert "drop table" in _strip_comments_and_quoted_text(query)


def test_pathological_detach_input_is_fast(write_mode_without_drop):
    """A single `DETACH .* PERMANENTLY` branch backtracked quadratically on
    repeated DETACH without PERMANENTLY, wedging executor threads."""
    pathological = "DETACH " * 20000

    start = time.monotonic()
    _validate_query_for_destructive_ops(pathological)
    with pytest.raises(ToolError):
        _validate_query_for_destructive_ops(pathological + "PERMANENTLY")
    assert time.monotonic() - start < 3.0
