"""Tests for the optional clickhouse-client config.yaml fallback.

Connection settings can be read from a clickhouse-client `config.yaml` pointed
at by CLICKHOUSE_CONFIG_FILE, as a fallback for unset environment variables.
"""

import os

import pytest

from mcp_clickhouse.mcp_env import ClickHouseConfig


def _write_config(tmp_path, content: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(content)
    return str(path)


def _clear_clickhouse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in [
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_DATABASE",
        "CLICKHOUSE_SECURE",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_CONFIG_FILE",
        "CLICKHOUSE_CONNECTION",
    ]:
        monkeypatch.delenv(var, raising=False)


def test_file_ignored_when_config_file_unset(monkeypatch: pytest.MonkeyPatch):
    """Without CLICKHOUSE_CONFIG_FILE, behavior is unchanged (env vars only)."""
    _clear_clickhouse_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_HOST", "envhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "envuser")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "envpass")

    config = ClickHouseConfig()

    assert config.host == "envhost"
    assert config.database is None
    assert config.secure is True


def test_env_vars_take_precedence_over_file(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Environment variables win over file values for the same setting."""
    _clear_clickhouse_env(monkeypatch)
    path = _write_config(
        tmp_path,
        "host: filehost\nuser: fileuser\npassword: filepass\n",
    )
    monkeypatch.setenv("CLICKHOUSE_CONFIG_FILE", path)
    monkeypatch.setenv("CLICKHOUSE_HOST", "envhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "envuser")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "envpass")

    config = ClickHouseConfig()

    assert config.host == "envhost"
    assert config.username == "envuser"
    assert config.password == "envpass"


def test_file_supplies_missing_values(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """host/user/password come from the file when the env vars are absent."""
    _clear_clickhouse_env(monkeypatch)
    path = _write_config(
        tmp_path,
        "host: filehost\nuser: fileuser\npassword: filepass\ndatabase: analytics\n",
    )
    monkeypatch.setenv("CLICKHOUSE_CONFIG_FILE", path)

    config = ClickHouseConfig()

    assert config.host == "filehost"
    assert config.username == "fileuser"
    assert config.password == "filepass"
    assert config.database == "analytics"


@pytest.mark.parametrize("secure_value", ["true", "1", "yes", "True"])
def test_secure_coercion_truthy(monkeypatch: pytest.MonkeyPatch, tmp_path, secure_value):
    """Various truthy `secure` forms in the file coerce to True."""
    _clear_clickhouse_env(monkeypatch)
    path = _write_config(
        tmp_path,
        f"host: h\nuser: u\npassword: p\nsecure: {secure_value}\n",
    )
    monkeypatch.setenv("CLICKHOUSE_CONFIG_FILE", path)

    config = ClickHouseConfig()

    assert config.secure is True


def test_secure_coercion_falsy(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A falsy `secure` form in the file coerces to False."""
    _clear_clickhouse_env(monkeypatch)
    path = _write_config(tmp_path, "host: h\nuser: u\npassword: p\nsecure: 0\n")
    monkeypatch.setenv("CLICKHOUSE_CONFIG_FILE", path)

    config = ClickHouseConfig()

    assert config.secure is False


def test_port_is_never_read_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """`port` in the file is ignored; port defaults from `secure` instead."""
    _clear_clickhouse_env(monkeypatch)
    path = _write_config(
        tmp_path,
        "host: h\nuser: u\npassword: p\nsecure: true\nport: 9000\n",
    )
    monkeypatch.setenv("CLICKHOUSE_CONFIG_FILE", path)

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    # secure=true -> default HTTPS port 8443, NOT the native 9000 from the file
    assert client_config["port"] == 8443
    assert client_config["secure"] is True


def test_named_connection_maps_hostname(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A connections_credentials entry is selected via CLICKHOUSE_CONNECTION."""
    _clear_clickhouse_env(monkeypatch)
    path = _write_config(
        tmp_path,
        """
connections_credentials:
  connection:
    - name: dev
      hostname: dev.example.com
      user: devuser
      password: devpass
      secure: 0
    - name: prod
      hostname: prod.example.com
      user: produser
      password: prodpass
      secure: 1
      database: prod_db
""",
    )
    monkeypatch.setenv("CLICKHOUSE_CONFIG_FILE", path)
    monkeypatch.setenv("CLICKHOUSE_CONNECTION", "prod")

    config = ClickHouseConfig()

    assert config.host == "prod.example.com"
    assert config.username == "produser"
    assert config.password == "prodpass"
    assert config.database == "prod_db"
    assert config.secure is True


def test_unknown_connection_name_raises(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Selecting a connection name absent from the file raises ValueError."""
    _clear_clickhouse_env(monkeypatch)
    path = _write_config(
        tmp_path,
        """
connections_credentials:
  connection:
    - name: dev
      hostname: dev.example.com
      user: devuser
      password: devpass
""",
    )
    monkeypatch.setenv("CLICKHOUSE_CONFIG_FILE", path)
    monkeypatch.setenv("CLICKHOUSE_CONNECTION", "nope")

    with pytest.raises(ValueError, match="nope"):
        ClickHouseConfig()


def test_missing_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A CLICKHOUSE_CONFIG_FILE that doesn't exist raises ValueError naming it."""
    _clear_clickhouse_env(monkeypatch)
    missing = str(tmp_path / "does-not-exist.yaml")
    monkeypatch.setenv("CLICKHOUSE_CONFIG_FILE", missing)

    with pytest.raises(ValueError, match="not found"):
        ClickHouseConfig()


def test_malformed_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A non-mapping / unparseable config file raises ValueError."""
    _clear_clickhouse_env(monkeypatch)
    path = _write_config(tmp_path, "- just\n- a\n- list\n")
    monkeypatch.setenv("CLICKHOUSE_CONFIG_FILE", path)

    with pytest.raises(ValueError, match="mapping"):
        ClickHouseConfig()


def test_file_provided_values_satisfy_validation(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """File-provided host/user/password satisfy required-var validation."""
    _clear_clickhouse_env(monkeypatch)
    path = _write_config(tmp_path, "host: h\nuser: u\npassword: p\n")
    monkeypatch.setenv("CLICKHOUSE_CONFIG_FILE", path)

    # Should not raise even though no CLICKHOUSE_HOST/USER/PASSWORD env vars are set.
    ClickHouseConfig()


def test_missing_required_without_file_raises(monkeypatch: pytest.MonkeyPatch):
    """With neither env vars nor a file, validation still raises."""
    _clear_clickhouse_env(monkeypatch)

    with pytest.raises(ValueError, match="CLICKHOUSE_HOST"):
        ClickHouseConfig()


def test_file_edit_is_picked_up_without_restart(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Editing the config file is reflected without recreating the config object."""
    _clear_clickhouse_env(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("host: host-a\nuser: u\npassword: p\n")
    monkeypatch.setenv("CLICKHOUSE_CONFIG_FILE", str(config_path))

    config = ClickHouseConfig()
    assert config.host == "host-a"

    # Rewrite the file with a bumped mtime; the cache must invalidate and re-read.
    config_path.write_text("host: host-b\nuser: u\npassword: p\n")
    new_mtime = config_path.stat().st_mtime_ns + 1_000_000_000
    os.utime(config_path, ns=(new_mtime, new_mtime))

    assert config.host == "host-b"
