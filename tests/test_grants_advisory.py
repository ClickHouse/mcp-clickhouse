"""Tests for the startup grants advisory.

Unit tests with a mocked client, no ClickHouse server. The advisory warns when
the drop gate is active but the connected user holds privileges the gate cannot
enforce against.
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import mcp_clickhouse.mcp_server as mcp_server
from mcp_clickhouse.mcp_server import _warn_if_overprivileged, create_clickhouse_client

LOGGER_NAME = "mcp-clickhouse"

_CLIENT_CONFIG = {
    "host": "localhost",
    "port": 8123,
    "username": "default",
    "secure": False,
    "verify": False,
    "connect_timeout": 1,
    "send_receive_timeout": 1,
}


@pytest.fixture(autouse=True)
def reset_advisory_flag():
    mcp_server._clear_client_cache()
    mcp_server._grants_advisory_done = False
    yield
    mcp_server._clear_client_cache()
    mcp_server._grants_advisory_done = False


def _client_with_grants(rows):
    client = MagicMock()
    client.query.return_value = SimpleNamespace(result_rows=rows)
    return client


def _config(allow_write_access: bool, allow_drop: bool) -> SimpleNamespace:
    return SimpleNamespace(
        allow_write_access=allow_write_access,
        allow_drop=allow_drop,
        get_client_config=lambda: dict(_CLIENT_CONFIG),
    )


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    "grant,privilege",
    [
        ("GRANT DROP ON *.* TO u", "DROP"),
        ("GRANT ALL ON *.* TO u WITH GRANT OPTION", "ALL"),
        ("GRANT TRUNCATE, DELETE ON d.* TO u", "TRUNCATE"),
        ("GRANT ALTER ON *.* TO u", "ALTER"),
        ("GRANT ALTER TABLE ON d.* TO u", "ALTER"),
    ],
)
def test_warns_on_dangerous_privilege(caplog, grant, privilege):
    client = _client_with_grants([(grant,)])
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        _warn_if_overprivileged(client)

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert privilege in warnings[0].getMessage()
    assert "README" in warnings[0].getMessage()


@pytest.mark.parametrize(
    "grant",
    [
        "GRANT SELECT, INSERT ON d.* TO u",
        # The README least-privilege recipe must not trigger the advisory.
        "GRANT SELECT, INSERT, CREATE TABLE, ALTER ADD COLUMN ON d.* TO u",
    ],
)
def test_no_warning_for_scoped_grants(caplog, grant):
    client = _client_with_grants([(grant,)])
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        _warn_if_overprivileged(client)

    assert not _warnings(caplog)


def test_role_grant_logged_at_info(caplog):
    client = _client_with_grants([("GRANT admin_role TO u",)])
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        _warn_if_overprivileged(client)

    assert not _warnings(caplog)
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("roles" in r.getMessage() and "admin_role" in r.getMessage() for r in infos)


def test_show_grants_failure_is_silent(caplog):
    client = MagicMock()
    client.query.side_effect = RuntimeError("boom")
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        _warn_if_overprivileged(client)

    assert not _warnings(caplog)


def test_advisory_runs_once():
    client = _client_with_grants([("GRANT DROP ON *.* TO u",)])
    _warn_if_overprivileged(client)
    _warn_if_overprivileged(client)

    assert client.query.call_count == 1


def test_advisory_runs_in_write_mode_without_drop(caplog):
    client = _client_with_grants([("GRANT DROP ON *.* TO u",)])
    config = _config(allow_write_access=True, allow_drop=False)
    with patch("mcp_clickhouse.mcp_server.get_config", return_value=config):
        with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client", return_value=client):
            with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
                assert create_clickhouse_client() is client

    client.query.assert_called_once_with("SHOW GRANTS")
    assert any("DROP" in r.getMessage() for r in _warnings(caplog))


def test_advisory_skipped_for_request_override_client(caplog):
    """An override client may connect as a different user. The advisory must
    neither inspect it nor consume the one-shot for the base user."""
    client = _client_with_grants([("GRANT DROP ON *.* TO u",)])
    config = _config(allow_write_access=True, allow_drop=False)
    with patch("mcp_clickhouse.mcp_server.get_config", return_value=config):
        with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client", return_value=client):
            assert create_clickhouse_client({"username": "other"}) is client
            client.query.assert_not_called()
            assert mcp_server._grants_advisory_done is False

            # A later base-config client still runs the advisory.
            with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
                create_clickhouse_client()

    client.query.assert_called_once_with("SHOW GRANTS")
    assert any("DROP" in r.getMessage() for r in _warnings(caplog))


@pytest.mark.parametrize(
    "allow_write_access,allow_drop",
    [(False, False), (False, True), (True, True)],
)
def test_advisory_skipped_by_config(allow_write_access, allow_drop):
    client = _client_with_grants([("GRANT DROP ON *.* TO u",)])
    config = _config(allow_write_access, allow_drop)
    with patch("mcp_clickhouse.mcp_server.get_config", return_value=config):
        with patch("mcp_clickhouse.mcp_server.clickhouse_connect.get_client", return_value=client):
            assert create_clickhouse_client() is client

    client.query.assert_not_called()
    assert mcp_server._grants_advisory_done is False
