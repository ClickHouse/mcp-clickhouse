"""Integration tests for the chDB-only security baseline.

These open a real in-memory chDB session through ``_init_chdb_client`` and
assert the baseline is actually enforced by the engine: ``SET readonly=2``
(unless writes are allowed), the result-byte / execution-time caps, and the
CHDB_FILE_ALLOWLIST sandbox over raw SQL. Skipped if chDB is not installed.
"""

from unittest.mock import patch

import pytest

from mcp_clickhouse import mcp_server

chdb = pytest.importorskip("chdb")


@pytest.fixture
def chdb_session(monkeypatch):
    """Yield a real in-memory chDB session with the baseline applied.

    The session is also wired in as ``mcp_server._chdb_client`` so the
    ``execute_chdb_query`` path can run against it, and closed afterwards.
    """
    monkeypatch.setenv("CHDB_ENABLED", "true")
    monkeypatch.setenv("CHDB_DATA_PATH", ":memory:")

    client = mcp_server._init_chdb_client()
    assert client is not None, mcp_server._chdb_error_message
    with patch.object(mcp_server, "_chdb_client", client):
        try:
            yield client
        finally:
            client.close()


def test_readonly_setting_is_2_by_default(chdb_session):
    out = str(chdb_session.query("SELECT value FROM system.settings WHERE name = 'readonly'", "CSV"))
    assert "2" in out


def test_readonly_blocks_ddl_by_default(chdb_session):
    result = mcp_server.execute_chdb_query("CREATE TABLE t (a Int32) ENGINE = Memory")
    assert isinstance(result, dict) and "error" in result


def test_max_result_bytes_setting_applied(chdb_session):
    out = str(
        chdb_session.query("SELECT value FROM system.settings WHERE name = 'max_result_bytes'", "CSV")
    )
    assert "1048576" in out


def test_writes_allowed_when_flag_set(monkeypatch):
    monkeypatch.setenv("CHDB_ENABLED", "true")
    monkeypatch.setenv("CHDB_DATA_PATH", ":memory:")
    monkeypatch.setenv("CHDB_ALLOW_WRITE_ACCESS", "true")
    client = mcp_server._init_chdb_client()
    assert client is not None, mcp_server._chdb_error_message
    try:
        out = str(client.query("SELECT value FROM system.settings WHERE name = 'readonly'", "CSV"))
        # readonly not forced to 2 when writes are allowed
        assert "2" not in out
    finally:
        client.close()


def test_select_passes_through(chdb_session):
    result = mcp_server.execute_chdb_query("SELECT 1 AS n")
    assert result == [{"n": 1}]


def test_allowlist_rejects_external_table_function(chdb_session, monkeypatch):
    monkeypatch.setenv("CHDB_FILE_ALLOWLIST", "/data")
    # url() must be refused before any network access is attempted.
    result = mcp_server.execute_chdb_query("SELECT * FROM url('http://169.254.169.254/', 'CSV')")
    assert isinstance(result, dict) and "error" in result
    assert "CHDB_FILE_ALLOWLIST" in result["error"]


def test_allowlist_allows_safe_query(chdb_session, monkeypatch):
    monkeypatch.setenv("CHDB_FILE_ALLOWLIST", "/data")
    result = mcp_server.execute_chdb_query("SELECT * FROM numbers(3)")
    assert result == [{"number": 0}, {"number": 1}, {"number": 2}]


def test_allowlist_is_a_path_allowlist(monkeypatch):
    # It's a path allowlist (DuckDB allowed_directories style), not a full sandbox:
    monkeypatch.setenv("CHDB_FILE_ALLOWLIST", "/data")
    # a file-like path UNDER an allowed prefix is permitted
    assert mcp_server._chdb_sql_source_violation("SELECT * FROM file('/data/x.parquet')") is None
    # a path OUTSIDE the allowlist is refused
    assert mcp_server._chdb_sql_source_violation("SELECT * FROM file('/etc/passwd')") is not None
    # a DSN-based source can't be path-checked -> refused while the allowlist is active
    assert mcp_server._chdb_sql_source_violation(
        "SELECT * FROM postgresql('h:5432', 'db', 't', 'u', 'p')"
    ) is not None


def test_no_gating_when_allowlist_unset(chdb_session, monkeypatch):
    monkeypatch.delenv("CHDB_FILE_ALLOWLIST", raising=False)
    # With no allowlist the scanner must not flag anything (0.4.0 behavior).
    assert mcp_server._chdb_sql_source_violation("SELECT * FROM url('http://x', 'CSV')") is None
