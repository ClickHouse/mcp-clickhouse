from types import SimpleNamespace

import pytest

from mcp_clickhouse import mcp_server
from mcp_clickhouse.mcp_env import ClickHouseConfig


TLS_ENV_VARS = (
    "CLICKHOUSE_CA_CERT",
    "CLICKHOUSE_CLIENT_CERT",
    "CLICKHOUSE_CLIENT_CERT_KEY",
    "CLICKHOUSE_TLS_MODE",
)


@pytest.fixture(autouse=True)
def clickhouse_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKHOUSE_ENABLED", "true")
    monkeypatch.setenv("CLICKHOUSE_HOST", "clickhouse.example.com")
    monkeypatch.setenv("CLICKHOUSE_USER", "mcp_user")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "secret")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "true")
    monkeypatch.setenv("CLICKHOUSE_VERIFY", "true")
    for name in TLS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_tls_properties_and_client_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKHOUSE_CA_CERT", "/certs/ca.pem")
    monkeypatch.setenv("CLICKHOUSE_CLIENT_CERT", "/certs/client.pem")
    monkeypatch.setenv("CLICKHOUSE_CLIENT_CERT_KEY", "/certs/client-key.pem")
    monkeypatch.setenv("CLICKHOUSE_TLS_MODE", "  StRiCt  ")

    config = ClickHouseConfig()
    client_config = config.get_client_config()

    assert config.ca_cert == "/certs/ca.pem"
    assert config.client_cert == "/certs/client.pem"
    assert config.client_cert_key == "/certs/client-key.pem"
    assert config.tls_mode == "strict"
    assert client_config["ca_cert"] == "/certs/ca.pem"
    assert client_config["client_cert"] == "/certs/client.pem"
    assert client_config["client_cert_key"] == "/certs/client-key.pem"
    assert client_config["tls_mode"] == "strict"
    assert client_config["password"] == "secret"


def test_tls_options_omitted_by_default():
    client_config = ClickHouseConfig().get_client_config()

    for name in ("ca_cert", "client_cert", "client_cert_key", "tls_mode"):
        assert name not in client_config
    assert client_config["password"] == "secret"


def test_ca_cert_without_client_certificate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKHOUSE_CA_CERT", "/certs/private-ca.pem")

    client_config = ClickHouseConfig().get_client_config()

    assert client_config["ca_cert"] == "/certs/private-ca.pem"
    assert "client_cert" not in client_config
    assert client_config["password"] == "secret"


@pytest.mark.parametrize("env_password", [None, "secret"])
@pytest.mark.parametrize("tls_mode", [None, "  MuTuAl  "])
def test_mutual_tls_does_not_require_or_pass_password(
    monkeypatch: pytest.MonkeyPatch, env_password: str | None, tls_mode: str | None
):
    monkeypatch.setenv("CLICKHOUSE_CLIENT_CERT", "/certs/client.pem")
    if env_password is None:
        monkeypatch.delenv("CLICKHOUSE_PASSWORD")
    else:
        monkeypatch.setenv("CLICKHOUSE_PASSWORD", env_password)
    if tls_mode is not None:
        monkeypatch.setenv("CLICKHOUSE_TLS_MODE", tls_mode)

    client_config = ClickHouseConfig().get_client_config()

    assert client_config["password"] == ""
    if tls_mode is None:
        assert "tls_mode" not in client_config
    else:
        assert client_config["tls_mode"] == "mutual"


@pytest.mark.parametrize("name", ["CLICKHOUSE_HOST", "CLICKHOUSE_USER"])
def test_mutual_tls_still_requires_host_and_user(monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.setenv("CLICKHOUSE_CLIENT_CERT", "/certs/client.pem")
    monkeypatch.delenv("CLICKHOUSE_PASSWORD")
    monkeypatch.delenv(name)

    with pytest.raises(ValueError, match=name):
        ClickHouseConfig()


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"CLICKHOUSE_CA_CERT": "/certs/private-ca.pem"},
        {"CLICKHOUSE_CLIENT_CERT": "/certs/client.pem", "CLICKHOUSE_TLS_MODE": "proxy"},
        {"CLICKHOUSE_CLIENT_CERT": "/certs/client.pem", "CLICKHOUSE_TLS_MODE": "strict"},
    ],
    ids=["no-tls", "ca-only", "proxy", "strict"],
)
def test_password_required_without_mutual_tls(monkeypatch: pytest.MonkeyPatch, env: dict):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLICKHOUSE_PASSWORD")

    with pytest.raises(ValueError, match="CLICKHOUSE_PASSWORD"):
        ClickHouseConfig()


def test_invalid_tls_mode_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKHOUSE_CLIENT_CERT", "/certs/client.pem")
    monkeypatch.setenv("CLICKHOUSE_TLS_MODE", "invalid")

    with pytest.raises(ValueError, match="CLICKHOUSE_TLS_MODE.*mutual, proxy, strict"):
        ClickHouseConfig()


def test_blank_tls_mode_is_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKHOUSE_CLIENT_CERT", "/certs/client.pem")
    monkeypatch.setenv("CLICKHOUSE_TLS_MODE", "  ")

    client_config = ClickHouseConfig().get_client_config()

    assert "tls_mode" not in client_config
    assert client_config["password"] == ""


def test_client_cert_key_requires_client_cert(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKHOUSE_CLIENT_CERT_KEY", "/certs/client-key.pem")

    with pytest.raises(ValueError, match="CLICKHOUSE_CLIENT_CERT_KEY.*CLICKHOUSE_CLIENT_CERT"):
        ClickHouseConfig()


def test_tls_mode_requires_client_cert(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKHOUSE_TLS_MODE", "mutual")

    with pytest.raises(ValueError, match="CLICKHOUSE_TLS_MODE.*CLICKHOUSE_CLIENT_CERT"):
        ClickHouseConfig()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CLICKHOUSE_CA_CERT", "/certs/ca.pem"),
        ("CLICKHOUSE_CLIENT_CERT", "/certs/client.pem"),
        ("CLICKHOUSE_CLIENT_CERT_KEY", "/certs/client-key.pem"),
        ("CLICKHOUSE_TLS_MODE", "mutual"),
    ],
)
def test_tls_options_require_secure_connection(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
):
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"{name}.*CLICKHOUSE_SECURE=true"):
        ClickHouseConfig()


def test_ca_cert_requires_certificate_verification(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKHOUSE_CA_CERT", "/certs/ca.pem")
    monkeypatch.setenv("CLICKHOUSE_VERIFY", "false")

    with pytest.raises(ValueError, match="CLICKHOUSE_CA_CERT.*CLICKHOUSE_VERIFY=true"):
        ClickHouseConfig()


@pytest.mark.parametrize(
    ("value", "expected", "readonly"),
    [(None, False, "1"), ("false", False, "1"), ("true", True, "0")],
)
def test_write_access_property_controls_readonly_setting(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    expected: bool,
    readonly: str,
):
    if value is None:
        monkeypatch.delenv("CLICKHOUSE_ALLOW_WRITE_ACCESS", raising=False)
    else:
        monkeypatch.setenv("CLICKHOUSE_ALLOW_WRITE_ACCESS", value)

    config = ClickHouseConfig()
    monkeypatch.setattr(mcp_server, "get_config", lambda: config)

    assert config.allow_write_access is expected
    assert mcp_server.build_query_settings(SimpleNamespace(server_settings={})) == {
        "readonly": readonly
    }
