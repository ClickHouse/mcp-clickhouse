"""Tests for Postgres environment configuration."""

import certifi
import pytest

from mcp_clickhouse.mcp_env import PostgresConfig

_POSTGRES_VARS = (
    "POSTGRES_ENABLED",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DATABASE",
    "POSTGRES_SSLMODE",
    "POSTGRES_SSLROOTCERT",
    "POSTGRES_CONNECT_TIMEOUT",
    "POSTGRES_ALLOW_WRITE_ACCESS",
    "POSTGRES_ALLOW_DROP",
)


@pytest.fixture
def postgres_env(monkeypatch):
    for name in _POSTGRES_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("POSTGRES_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_HOST", "pg.example.com")
    monkeypatch.setenv("POSTGRES_USER", "reader")
    return monkeypatch


def test_disabled_by_default_without_required_vars(monkeypatch):
    for name in _POSTGRES_VARS:
        monkeypatch.delenv(name, raising=False)

    config = PostgresConfig()

    assert config.enabled is False


def test_defaults_are_secure(postgres_env):
    config = PostgresConfig()

    assert config.get_connect_kwargs() == {
        "host": "pg.example.com",
        "port": 5432,
        "user": "reader",
        "sslmode": "verify-full",
        "sslrootcert": certifi.where(),
        "connect_timeout": 30,
        "application_name": "mcp_clickhouse",
    }
    assert config.allow_write_access is False
    assert config.allow_drop is False


def test_optional_values_reach_connect_kwargs(postgres_env):
    postgres_env.setenv("POSTGRES_PORT", "6543")
    postgres_env.setenv("POSTGRES_PASSWORD", "secret")
    postgres_env.setenv("POSTGRES_DATABASE", "app")
    postgres_env.setenv("POSTGRES_SSLROOTCERT", "/etc/ssl/pg-ca.pem")
    postgres_env.setenv("POSTGRES_CONNECT_TIMEOUT", "5")

    kwargs = PostgresConfig().get_connect_kwargs()

    assert kwargs["port"] == 6543
    assert kwargs["password"] == "secret"
    assert kwargs["dbname"] == "app"
    assert kwargs["sslrootcert"] == "/etc/ssl/pg-ca.pem"
    assert kwargs["connect_timeout"] == 5


def test_empty_password_is_passed_through(postgres_env):
    postgres_env.setenv("POSTGRES_PASSWORD", "")

    assert PostgresConfig().get_connect_kwargs()["password"] == ""


@pytest.mark.parametrize(
    ("sslmode", "expects_root_cert"),
    [
        ("disable", False),
        ("allow", False),
        ("prefer", False),
        ("require", False),
        ("verify-ca", True),
        ("VERIFY-FULL", True),
    ],
)
def test_default_root_cert_only_for_verifying_modes(postgres_env, sslmode, expects_root_cert):
    postgres_env.setenv("POSTGRES_SSLMODE", sslmode)

    kwargs = PostgresConfig().get_connect_kwargs()

    assert kwargs["sslmode"] == sslmode.lower()
    assert ("sslrootcert" in kwargs) is expects_root_cert


def test_invalid_sslmode_is_rejected(postgres_env):
    postgres_env.setenv("POSTGRES_SSLMODE", "on")

    with pytest.raises(ValueError, match="POSTGRES_SSLMODE must be one of"):
        PostgresConfig()


@pytest.mark.parametrize("missing", ["POSTGRES_HOST", "POSTGRES_USER"])
def test_required_vars(postgres_env, missing):
    postgres_env.delenv(missing)

    with pytest.raises(ValueError, match=missing):
        PostgresConfig()


@pytest.mark.parametrize(
    ("write", "drop"),
    [("true", "false"), ("TRUE", "true"), ("false", "true")],
)
def test_write_flags(postgres_env, write, drop):
    postgres_env.setenv("POSTGRES_ALLOW_WRITE_ACCESS", write)
    postgres_env.setenv("POSTGRES_ALLOW_DROP", drop)

    config = PostgresConfig()

    assert config.allow_write_access is (write.lower() == "true")
    assert config.allow_drop is (drop == "true")


@pytest.mark.parametrize("name", ["POSTGRES_PORT", "POSTGRES_CONNECT_TIMEOUT"])
def test_integer_settings_are_validated_at_startup(postgres_env, name):
    postgres_env.setenv(name, "abc")

    with pytest.raises(ValueError, match=f"{name} must be an integer"):
        PostgresConfig()
