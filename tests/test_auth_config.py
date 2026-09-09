import inspect
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.azure import AzureProvider

import mcp_clickhouse.mcp_server as mcp_server_module
from mcp_clickhouse.mcp_env import MCPServerConfig
from mcp_clickhouse.mcp_server import _load_fastmcp_auth_provider, _resolve_auth
from mcp_clickhouse.mcp_server import (
    _LEGACY_AUTH_PROVIDER_ENV,
    _get_case_insensitive_value,
    _load_default_dotenv,
    _parse_auth_provider_env_value,
)


class RecordingAuthProvider(AuthProvider):
    def __init__(self, **kwargs):
        self.kwargs = kwargs


_EXPECTED_LEGACY_AUTH_PROVIDER_PATHS = {
    "fastmcp.server.auth.providers.auth0.Auth0Provider",
    "fastmcp.server.auth.providers.aws.AWSCognitoProvider",
    "fastmcp.server.auth.providers.azure.AzureProvider",
    "fastmcp.server.auth.providers.descope.DescopeProvider",
    "fastmcp.server.auth.providers.discord.DiscordProvider",
    "fastmcp.server.auth.providers.github.GitHubProvider",
    "fastmcp.server.auth.providers.google.GoogleProvider",
    "fastmcp.server.auth.providers.introspection.IntrospectionTokenVerifier",
    "fastmcp.server.auth.providers.jwt.JWTVerifier",
    "fastmcp.server.auth.providers.oci.OCIProvider",
    "fastmcp.server.auth.providers.scalekit.ScalekitProvider",
    "fastmcp.server.auth.providers.supabase.SupabaseProvider",
    "fastmcp.server.auth.providers.workos.WorkOSProvider",
    "fastmcp.server.auth.providers.workos.AuthKitProvider",
}


def test_auth_token_configuration(monkeypatch: pytest.MonkeyPatch):
    """Test that auth token is correctly configured when set."""
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "test-secret-token")

    config = MCPServerConfig()

    assert config.auth_token == "test-secret-token"
    assert config.auth_disabled is False


def test_auth_disabled_configuration(monkeypatch: pytest.MonkeyPatch):
    """Test that auth can be disabled when CLICKHOUSE_MCP_AUTH_DISABLED=true."""
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_TOKEN", raising=False)

    config = MCPServerConfig()

    assert config.auth_disabled is True
    assert config.auth_token is None


def test_auth_enabled_by_default(monkeypatch: pytest.MonkeyPatch):
    """Test that auth is enabled by default (auth_disabled=False)."""
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("CLICKHOUSE_MCP_AUTH_TOKEN", raising=False)

    config = MCPServerConfig()

    assert config.auth_disabled is False
    assert config.auth_token is None


def test_auth_token_with_stdio_transport(monkeypatch: pytest.MonkeyPatch):
    """Test that auth token is available but not required for stdio transport."""
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "stdio")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "test-token")

    config = MCPServerConfig()

    assert config.server_transport == "stdio"
    assert config.auth_token == "test-token"


def test_auth_token_with_http_transport(monkeypatch: pytest.MonkeyPatch):
    """Test that auth token is correctly configured for HTTP transport."""
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "http-auth-token")

    config = MCPServerConfig()

    assert config.server_transport == "http"
    assert config.auth_token == "http-auth-token"
    assert config.auth_disabled is False


def test_auth_token_with_sse_transport(monkeypatch: pytest.MonkeyPatch):
    """Test that auth token is correctly configured for SSE transport."""
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "sse")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "sse-auth-token")

    config = MCPServerConfig()

    assert config.server_transport == "sse"
    assert config.auth_token == "sse-auth-token"
    assert config.auth_disabled is False


def _clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    auth_names = {
        name
        for name in mcp_server_module.os.environ
        if name.casefold().startswith("fastmcp_server_auth")
        or name.casefold() == "fastmcp_env_file"
    }
    auth_names.update(
        {
            "FASTMCP_SERVER_AUTH",
            "fastmcp_server_auth",
            "FASTMCP_ENV_FILE",
            "fastmcp_env_file",
        }
    )
    for prefix, fields in _LEGACY_AUTH_PROVIDER_ENV.values():
        for field_name in fields:
            env_name = f"FASTMCP_SERVER_AUTH_{prefix}{field_name.upper()}"
            auth_names.update({env_name, env_name.lower()})
    for var in auth_names | {
        "CLICKHOUSE_MCP_AUTH_TOKEN",
        "CLICKHOUSE_MCP_AUTH_DISABLED",
    }:
        monkeypatch.delenv(var, raising=False)


def _delete_loaded_auth_env() -> None:
    for name in tuple(mcp_server_module.os.environ):
        if name.casefold().startswith("fastmcp_server_auth") or (
            name.casefold() == "fastmcp_env_file"
        ):
            del mcp_server_module.os.environ[name]


def test_resolve_auth_loads_oauth_provider(monkeypatch: pytest.MonkeyPatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH", "fastmcp.server.auth.providers.jwt.JWTVerifier")
    provider = RecordingAuthProvider()
    monkeypatch.setattr(
        mcp_server_module,
        "_load_fastmcp_auth_provider",
        lambda _provider_path, **_kwargs: provider,
    )

    assert _resolve_auth(MCPServerConfig()) == {"auth": provider}


@pytest.mark.parametrize(
    ("env_file_name", "selector_name"),
    [
        ("FASTMCP_ENV_FILE", "FASTMCP_SERVER_AUTH"),
        ("fastmcp_env_file", "fastmcp_server_auth"),
    ],
)
def test_resolve_auth_loads_provider_selector_from_explicit_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    env_file_name,
    selector_name,
):
    _clear_auth_env(monkeypatch)
    provider_path = "fastmcp.server.auth.providers.jwt.JWTVerifier"
    env_file = tmp_path / "fastmcp.env"
    field_name = (
        "fastmcp_server_auth_jwt_jwks_uri"
        if selector_name.islower()
        else "FASTMCP_SERVER_AUTH_JWT_JWKS_URI"
    )
    env_file.write_text(
        f"{selector_name}={provider_path}\n"
        f"{field_name}=https://explicit.example/jwks.json\n"
    )
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv(env_file_name, str(env_file))
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(JWTVerifier=RecordingAuthProvider),
    )

    resolved = _resolve_auth(MCPServerConfig())

    assert resolved["auth"].kwargs["jwks_uri"] == "https://explicit.example/jwks.json"


def test_resolve_auth_loads_lowercase_provider_selector(monkeypatch: pytest.MonkeyPatch):
    _clear_auth_env(monkeypatch)
    provider_path = "example.auth.CustomProvider"
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("fastmcp_server_auth", provider_path)
    provider = RecordingAuthProvider()
    load_provider = MagicMock(return_value=provider)
    monkeypatch.setattr(mcp_server_module, "_load_fastmcp_auth_provider", load_provider)

    assert _resolve_auth(MCPServerConfig()) == {"auth": provider}
    assert load_provider.call_args.args == (provider_path,)


def test_explicit_auth_file_does_not_pollute_clickhouse_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    _clear_auth_env(monkeypatch)
    env_file = tmp_path / "fastmcp.env"
    env_file.write_text(
        "FASTMCP_SERVER_AUTH=example.auth.CustomProvider\n"
        "CLICKHOUSE_MCP_AUTH_DISABLED=true\n"
    )
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("FASTMCP_ENV_FILE", str(env_file))
    provider = RecordingAuthProvider()
    monkeypatch.setattr(
        mcp_server_module,
        "_load_fastmcp_auth_provider",
        MagicMock(return_value=provider),
    )

    assert _resolve_auth(MCPServerConfig()) == {"auth": provider}
    assert "CLICKHOUSE_MCP_AUTH_DISABLED" not in mcp_server_module.os.environ


def test_default_dotenv_preserves_process_auth_and_loads_noncolliding_auth(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("fastmcp_server_auth", "process.Provider")
    monkeypatch.setenv("fastmcp_server_auth_jwt_jwks_uri", "process-jwks")
    monkeypatch.setenv("FASTMCP_ENV_FILE", "process.env")

    def load_values(**_kwargs):
        monkeypatch.setenv("FASTMCP_SERVER_AUTH", "file.Provider")
        monkeypatch.setenv("FASTMCP_SERVER_AUTH_JWT_JWKS_URI", "file-jwks")
        monkeypatch.setenv("FASTMCP_SERVER_AUTH_JWT_AUDIENCE", "file-audience")
        monkeypatch.setenv("fastmcp_env_file", "file.env")
        monkeypatch.setenv("CLICKHOUSE_HOST", "file-host")

    load_default = MagicMock(side_effect=load_values)
    monkeypatch.setattr(mcp_server_module, "load_dotenv", load_default)
    monkeypatch.setattr(
        mcp_server_module,
        "_find_default_dotenv",
        lambda: "/package/.env",
    )

    _load_default_dotenv()

    load_default.assert_called_once_with(dotenv_path="/package/.env")
    assert mcp_server_module.os.environ["fastmcp_server_auth"] == "process.Provider"
    assert (
        mcp_server_module.os.environ["fastmcp_server_auth_jwt_jwks_uri"]
        == "process-jwks"
    )
    assert mcp_server_module.os.environ["FASTMCP_ENV_FILE"] == "process.env"
    assert "FASTMCP_SERVER_AUTH" not in mcp_server_module.os.environ
    assert "FASTMCP_SERVER_AUTH_JWT_JWKS_URI" not in mcp_server_module.os.environ
    assert mcp_server_module.os.environ["FASTMCP_SERVER_AUTH_JWT_AUDIENCE"] == "file-audience"
    assert "fastmcp_env_file" not in mcp_server_module.os.environ
    assert mcp_server_module.os.environ["CLICKHOUSE_HOST"] == "file-host"


def test_default_dotenv_delegates_python_dotenv_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    _clear_auth_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("CLICKHOUSE_HOST=file-host\n")
    monkeypatch.delenv("CLICKHOUSE_HOST", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "true")
    monkeypatch.setattr(
        mcp_server_module,
        "_find_default_dotenv",
        lambda: str(env_file),
    )

    _load_default_dotenv()

    assert "CLICKHOUSE_HOST" not in mcp_server_module.os.environ


def test_default_dotenv_discovery_walks_up_from_resolved_package_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    package_dir = tmp_path / "source" / "mcp_clickhouse"
    package_dir.mkdir(parents=True)
    (package_dir / ".env").mkdir()
    env_file = package_dir.parent / ".env"
    env_file.write_text("CLICKHOUSE_HOST=package-host\n")
    monkeypatch.setattr(
        mcp_server_module,
        "__file__",
        str(package_dir / "mcp_server.py"),
    )

    assert mcp_server_module._find_default_dotenv() == str(env_file)


def test_default_dotenv_discovery_returns_empty_path_when_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(mcp_server_module.os.path, "isfile", lambda _path: False)

    assert mcp_server_module._find_default_dotenv() == ""


def _copy_isolated_package(tmp_path):
    source_root = tmp_path / "source"
    package_dir = source_root / "mcp_clickhouse"
    shutil.copytree(
        os.path.dirname(mcp_server_module.__file__),
        package_dir,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return source_root


def _isolated_launch_env(source_root):
    child_env = {}
    for name, value in os.environ.items():
        normalized_name = name.casefold()
        if normalized_name.startswith("fastmcp_") or normalized_name.startswith(
            "python_dotenv"
        ):
            continue
        if normalized_name in {
            "clickhouse_mcp_auth_token",
            "clickhouse_mcp_auth_disabled",
        }:
            continue
        child_env[name] = value
    child_env.update(
        {
            "PYTHONPATH": str(source_root),
            "CLICKHOUSE_MCP_SERVER_TRANSPORT": "http",
            "MCP_CLICKHOUSE_TRUSTSTORE_DISABLE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return child_env


_IMPORT_COMMANDS = (
    ("-m", "mcp_clickhouse.main"),
    (
        "-c",
        "import os; import mcp_clickhouse.mcp_server; "
        "print(os.getenv('FASTMCP_SERVER_AUTH'))",
    ),
)


@pytest.mark.parametrize("import_command", _IMPORT_COMMANDS, ids=("module", "command"))
def test_import_modes_ignore_hostile_cwd_dotenv_and_use_package_discovery(
    tmp_path,
    import_command,
):
    source_root = _copy_isolated_package(tmp_path)
    trusted_provider = "trusted.example.MissingProvider"
    hostile_provider = "hostile.example.MissingProvider"
    (source_root / ".env").write_text(f"FASTMCP_SERVER_AUTH={trusted_provider}\n")
    hostile_dir = tmp_path / "hostile"
    hostile_dir.mkdir()
    (hostile_dir / ".env").write_text(
        f"FASTMCP_SERVER_AUTH={hostile_provider}\n"
        "FASTMCP_SERVER_AUTH_JWT_PUBLIC_KEY=hostile-key\n"
    )

    result = subprocess.run(
        [sys.executable, *import_command],
        cwd=hostile_dir,
        env=_isolated_launch_env(source_root),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    output = result.stdout + result.stderr

    if import_command[0] == "-m":
        assert result.returncode != 0
    else:
        assert result.returncode == 0
    assert trusted_provider in output
    assert hostile_provider not in output


@pytest.mark.parametrize("import_command", _IMPORT_COMMANDS, ids=("module", "command"))
def test_import_modes_without_package_dotenv_fail_closed_from_hostile_cwd(
    tmp_path,
    import_command,
):
    source_root = _copy_isolated_package(tmp_path)
    hostile_provider = "hostile.example.MissingProvider"
    hostile_dir = tmp_path / "hostile"
    hostile_dir.mkdir()
    (hostile_dir / ".env").write_text(
        f"FASTMCP_SERVER_AUTH={hostile_provider}\n"
        "FASTMCP_SERVER_AUTH_JWT_PUBLIC_KEY=hostile-key\n"
    )

    result = subprocess.run(
        [sys.executable, *import_command],
        cwd=hostile_dir,
        env=_isolated_launch_env(source_root),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    output = result.stdout + result.stderr

    if import_command[0] == "-m":
        assert result.returncode != 0
        assert "Authentication is required for HTTP/SSE transports" in output
    else:
        assert result.returncode == 0
        assert result.stdout.strip() == "None"
    assert hostile_provider not in output


def test_module_dotenv_auth_loads_from_foreign_working_directory_without_redirect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    _clear_auth_env(monkeypatch)
    provider_path = "fastmcp.server.auth.providers.jwt.JWTVerifier"
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    redirected_file = tmp_path / "redirected.env"
    redirected_file.write_text(
        f"FASTMCP_SERVER_AUTH={provider_path}\n"
        "FASTMCP_SERVER_AUTH_JWT_JWKS_URI=https://redirected.example/jwks.json\n"
    )
    module_file = tmp_path / "module.env"
    module_file.write_text(
        f"FASTMCP_ENV_FILE={redirected_file}\n"
        f"FASTMCP_SERVER_AUTH={provider_path}\n"
        "FASTMCP_SERVER_AUTH_JWT_JWKS_URI=https://first.example/jwks.json\n"
        "fastmcp_server_auth_jwt_jwks_uri=https://module.example/jwks.json\n"
    )
    monkeypatch.chdir(launch_dir)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setattr(
        mcp_server_module,
        "_find_default_dotenv",
        lambda: str(module_file),
    )
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(JWTVerifier=RecordingAuthProvider),
    )

    try:
        _load_default_dotenv()
        resolved = _resolve_auth(MCPServerConfig())

        assert "FASTMCP_ENV_FILE" not in mcp_server_module.os.environ
        assert mcp_server_module.os.environ["FASTMCP_SERVER_AUTH"] == provider_path
        assert resolved["auth"].kwargs["jwks_uri"] == "https://module.example/jwks.json"
    finally:
        _delete_loaded_auth_env()


@pytest.mark.parametrize("explicit", [False, True])
def test_lowercase_process_auth_value_wins_over_uppercase_file_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    explicit,
):
    _clear_auth_env(monkeypatch)
    provider_path = "fastmcp.server.auth.providers.jwt.JWTVerifier"
    env_file = tmp_path / ("fastmcp.env" if explicit else ".env")
    env_file.write_text(
        "FASTMCP_SERVER_AUTH_JWT_JWKS_URI=https://file.example/jwks.json\n"
    )
    if explicit:
        monkeypatch.setenv("FASTMCP_ENV_FILE", str(env_file))
    else:
        monkeypatch.delenv("FASTMCP_ENV_FILE", raising=False)
        monkeypatch.setattr(
            mcp_server_module,
            "_find_default_dotenv",
            lambda: str(env_file),
        )
    monkeypatch.setenv(
        "fastmcp_server_auth_jwt_jwks_uri",
        "https://process.example/jwks.json",
    )
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(JWTVerifier=RecordingAuthProvider),
    )
    try:
        if not explicit:
            _load_default_dotenv()

        provider = _load_fastmcp_auth_provider(provider_path)

        assert provider.kwargs["jwks_uri"] == "https://process.example/jwks.json"
        assert "FASTMCP_SERVER_AUTH_JWT_JWKS_URI" not in mcp_server_module.os.environ
    finally:
        _delete_loaded_auth_env()


def test_cwd_dotenv_provider_selector_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    _clear_auth_env(monkeypatch)
    provider_path = "fastmcp.server.auth.providers.jwt.JWTVerifier"
    (tmp_path / ".env").write_text(
        f"FASTMCP_SERVER_AUTH={provider_path}\n"
        "FASTMCP_SERVER_AUTH_JWT_JWKS_URI=https://implicit.example/jwks.json\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    with pytest.raises(ValueError, match="Authentication is required") as exc_info:
        _resolve_auth(MCPServerConfig())

    assert "https://implicit.example/jwks.json" not in str(exc_info.value)
    assert "FASTMCP_SERVER_AUTH" not in mcp_server_module.os.environ


@pytest.mark.parametrize("selector_source", ["process", "module"])
def test_trusted_selector_uses_cwd_dotenv_provider_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    selector_source,
):
    _clear_auth_env(monkeypatch)
    provider_path = "fastmcp.server.auth.providers.jwt.JWTVerifier"
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    (launch_dir / ".env").write_text(
        "FASTMCP_SERVER_AUTH_JWT_JWKS_URI=https://cwd.example/jwks.json\n"
    )
    monkeypatch.chdir(launch_dir)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    try:
        if selector_source == "process":
            monkeypatch.setenv("FASTMCP_SERVER_AUTH", provider_path)
        else:
            module_file = tmp_path / "module.env"
            module_file.write_text(f"FASTMCP_SERVER_AUTH={provider_path}\n")
            monkeypatch.setattr(
                mcp_server_module,
                "_find_default_dotenv",
                lambda: str(module_file),
            )
            _load_default_dotenv()
        monkeypatch.setattr(
            mcp_server_module.importlib,
            "import_module",
            lambda _module_name: SimpleNamespace(JWTVerifier=RecordingAuthProvider),
        )

        resolved = _resolve_auth(MCPServerConfig())

        assert resolved["auth"].kwargs["jwks_uri"] == "https://cwd.example/jwks.json"
    finally:
        _delete_loaded_auth_env()


@pytest.mark.parametrize(
    "values",
    [
        {"FASTMCP_SERVER_AUTH": "first", "fastmcp_server_auth": "second"},
        {"fastmcp_server_auth": "first", "FASTMCP_SERVER_AUTH": "second"},
    ],
)
def test_case_insensitive_auth_lookup_uses_last_matching_key(values):
    assert _get_case_insensitive_value(values, "FASTMCP_SERVER_AUTH") == "second"


def test_load_auth_provider_preserves_legacy_azure_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    provider_path = "fastmcp.server.auth.providers.azure.AzureProvider"
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(AzureProvider=RecordingAuthProvider),
    )
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_AZURE_CLIENT_ID", "client-id")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_AZURE_CLIENT_SECRET", "secret-value")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_AZURE_TENANT_ID", "tenant-id")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_AZURE_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv(
        "FASTMCP_SERVER_AUTH_AZURE_REQUIRED_SCOPES",
        "read access_as_user",
    )
    monkeypatch.setenv(
        "FASTMCP_SERVER_AUTH_AZURE_ADDITIONAL_AUTHORIZE_SCOPES",
        '["openid", "profile"]',
    )
    monkeypatch.setenv(
        "FASTMCP_SERVER_AUTH_AZURE_ALLOWED_CLIENT_REDIRECT_URIS",
        '["https://client.example/callback"]',
    )

    provider = _load_fastmcp_auth_provider(provider_path)

    assert provider.kwargs == {
        "client_id": "client-id",
        "client_secret": "secret-value",
        "tenant_id": "tenant-id",
        "base_url": "https://mcp.example.com",
        "required_scopes": ["read", "access_as_user"],
        "additional_authorize_scopes": ["openid", "profile"],
        "allowed_client_redirect_uris": ["https://client.example/callback"],
    }


def test_documented_azure_environment_constructs_real_provider(
    monkeypatch: pytest.MonkeyPatch,
):
    for field_name in _LEGACY_AUTH_PROVIDER_ENV[
        "fastmcp.server.auth.providers.azure.AzureProvider"
    ][1]:
        monkeypatch.delenv(f"FASTMCP_SERVER_AUTH_AZURE_{field_name.upper()}", raising=False)
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_AZURE_CLIENT_ID", "client-id")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_AZURE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_AZURE_TENANT_ID", "tenant-id")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_AZURE_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_AZURE_REQUIRED_SCOPES", "read access_as_user")

    provider = _load_fastmcp_auth_provider(
        "fastmcp.server.auth.providers.azure.AzureProvider"
    )

    assert isinstance(provider, AzureProvider)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("null", None),
        ('"openid profile"', ["openid", "profile"]),
        ('["openid", "profile"]', ["openid", "profile"]),
        ("openid,profile", ["openid", "profile"]),
        ("openid profile", ["openid", "profile"]),
    ],
)
def test_legacy_scope_value_conversion(value, expected):
    assert (
        _parse_auth_provider_env_value(
            "fastmcp.server.auth.providers.azure.AzureProvider",
            "required_scopes",
            "FASTMCP_SERVER_AUTH_AZURE_REQUIRED_SCOPES",
            value,
        )
        == expected
    )


@pytest.mark.parametrize("value", ["true", "10", "{}", '["openid", true]'])
def test_legacy_scope_value_rejects_non_string_json_without_exposing_value(value):
    with pytest.raises(ValueError) as exc_info:
        _parse_auth_provider_env_value(
            "fastmcp.server.auth.providers.azure.AzureProvider",
            "required_scopes",
            "FASTMCP_SERVER_AUTH_AZURE_REQUIRED_SCOPES",
            value,
        )

    assert value not in str(exc_info.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("null", None), ('["https://client.example/callback"]', ["https://client.example/callback"])],
)
def test_legacy_redirect_uri_value_conversion(value, expected):
    assert (
        _parse_auth_provider_env_value(
            "fastmcp.server.auth.providers.github.GitHubProvider",
            "allowed_client_redirect_uris",
            "FASTMCP_SERVER_AUTH_GITHUB_ALLOWED_CLIENT_REDIRECT_URIS",
            value,
        )
        == expected
    )


@pytest.mark.parametrize("value", ["true", "10", "{}", '"https://client.example"'])
def test_legacy_redirect_uri_value_rejects_non_list_json(value):
    with pytest.raises(ValueError) as exc_info:
        _parse_auth_provider_env_value(
            "fastmcp.server.auth.providers.github.GitHubProvider",
            "allowed_client_redirect_uris",
            "FASTMCP_SERVER_AUTH_GITHUB_ALLOWED_CLIENT_REDIRECT_URIS",
            value,
        )

    assert value not in str(exc_info.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://issuer.example", "https://issuer.example"),
        ('"https://issuer.example"', "https://issuer.example"),
        ('["issuer-a", "issuer-b"]', ["issuer-a", "issuer-b"]),
        ("null", None),
    ],
)
def test_legacy_jwt_issuer_value_conversion(value, expected):
    assert (
        _parse_auth_provider_env_value(
            "fastmcp.server.auth.providers.jwt.JWTVerifier",
            "issuer",
            "FASTMCP_SERVER_AUTH_JWT_ISSUER",
            value,
        )
        == expected
    )


@pytest.mark.parametrize("value", ["true", "10", "{}", '["issuer", false]'])
def test_legacy_jwt_issuer_rejects_non_string_json(value):
    with pytest.raises(ValueError) as exc_info:
        _parse_auth_provider_env_value(
            "fastmcp.server.auth.providers.jwt.JWTVerifier",
            "issuer",
            "FASTMCP_SERVER_AUTH_JWT_ISSUER",
            value,
        )

    assert value not in str(exc_info.value)


def test_legacy_timeout_accepts_integral_decimal_string():
    assert (
        _parse_auth_provider_env_value(
            "fastmcp.server.auth.providers.github.GitHubProvider",
            "timeout_seconds",
            "FASTMCP_SERVER_AUTH_GITHUB_TIMEOUT_SECONDS",
            "10.0",
        )
        == 10
    )


def test_legacy_timeout_rejects_fractional_decimal_without_exposing_value():
    value = "10.5"
    with pytest.raises(ValueError) as exc_info:
        _parse_auth_provider_env_value(
            "fastmcp.server.auth.providers.github.GitHubProvider",
            "timeout_seconds",
            "FASTMCP_SERVER_AUTH_GITHUB_TIMEOUT_SECONDS",
            value,
        )

    assert value not in str(exc_info.value)


def test_legacy_auth_provider_map_matches_fastmcp4_signatures():
    assert set(_LEGACY_AUTH_PROVIDER_ENV) == _EXPECTED_LEGACY_AUTH_PROVIDER_PATHS
    assert sum(len(fields) for _, fields in _LEGACY_AUTH_PROVIDER_ENV.values()) == 110

    for provider_path, (_, fields) in _LEGACY_AUTH_PROVIDER_ENV.items():
        module_name, _, class_name = provider_path.rpartition(".")
        provider_class = getattr(mcp_server_module.importlib.import_module(module_name), class_name)
        assert issubclass(provider_class, AuthProvider)
        assert set(fields) <= set(inspect.signature(provider_class).parameters)


def test_legacy_auth_environment_is_case_insensitive(monkeypatch: pytest.MonkeyPatch):
    provider_path = "fastmcp.server.auth.providers.jwt.JWTVerifier"
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(JWTVerifier=RecordingAuthProvider),
    )
    monkeypatch.setenv(
        "fastmcp_server_auth_jwt_jwks_uri",
        "https://lowercase.example/jwks.json",
    )

    provider = _load_fastmcp_auth_provider(provider_path)

    assert provider.kwargs["jwks_uri"] == "https://lowercase.example/jwks.json"


def test_process_auth_environment_uses_last_matching_case_variant(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_auth_env(monkeypatch)
    provider_path = "fastmcp.server.auth.providers.jwt.JWTVerifier"
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(JWTVerifier=RecordingAuthProvider),
    )
    monkeypatch.setenv(
        "fastmcp_server_auth_jwt_jwks_uri",
        "https://lowercase.example/jwks.json",
    )
    monkeypatch.setenv(
        "FASTMCP_SERVER_AUTH_JWT_JWKS_URI",
        "https://uppercase.example/jwks.json",
    )

    provider = _load_fastmcp_auth_provider(provider_path)

    assert provider.kwargs["jwks_uri"] == "https://uppercase.example/jwks.json"


def test_legacy_auth_provider_loads_explicit_fastmcp_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    provider_path = "fastmcp.server.auth.providers.jwt.JWTVerifier"
    env_name = "FASTMCP_SERVER_AUTH_JWT_JWKS_URI"
    env_file = tmp_path / "fastmcp.env"
    env_file.write_text(f"{env_name}=https://env-file.example/jwks.json\n")
    monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("FASTMCP_ENV_FILE", str(env_file))
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(JWTVerifier=RecordingAuthProvider),
    )

    provider = _load_fastmcp_auth_provider(provider_path)

    assert provider.kwargs["jwks_uri"] == "https://env-file.example/jwks.json"


def test_auth_provider_constructor_error_reports_type_without_secret(
    monkeypatch: pytest.MonkeyPatch,
):
    class ProviderConfigurationError(Exception):
        pass

    class RejectingProvider(AuthProvider):
        def __init__(self, **kwargs):
            raise ProviderConfigurationError(kwargs["client_secret"])

    provider_path = "fastmcp.server.auth.providers.github.GitHubProvider"
    secret = "constructor-secret-value"
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_GITHUB_CLIENT_SECRET", secret)
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(GitHubProvider=RejectingProvider),
    )

    with pytest.raises(ValueError) as exc_info:
        _load_fastmcp_auth_provider(provider_path)

    assert "ProviderConfigurationError" in str(exc_info.value)
    assert secret not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize(
    ("provider_path", "class_name", "env_prefix", "default_scopes"),
    [
        (
            "fastmcp.server.auth.providers.auth0.Auth0Provider",
            "Auth0Provider",
            "AUTH0",
            ["openid"],
        ),
        (
            "fastmcp.server.auth.providers.aws.AWSCognitoProvider",
            "AWSCognitoProvider",
            "AWS_COGNITO",
            ["openid"],
        ),
        (
            "fastmcp.server.auth.providers.discord.DiscordProvider",
            "DiscordProvider",
            "DISCORD",
            ["identify"],
        ),
        (
            "fastmcp.server.auth.providers.github.GitHubProvider",
            "GitHubProvider",
            "GITHUB",
            ["user"],
        ),
        (
            "fastmcp.server.auth.providers.google.GoogleProvider",
            "GoogleProvider",
            "GOOGLE",
            ["openid"],
        ),
        (
            "fastmcp.server.auth.providers.oci.OCIProvider",
            "OCIProvider",
            "OCI",
            ["openid"],
        ),
    ],
)
@pytest.mark.parametrize("configured_scopes", ["", "[]", "null"])
def test_legacy_empty_scopes_restore_provider_defaults(
    monkeypatch,
    provider_path,
    class_name,
    env_prefix,
    default_scopes,
    configured_scopes,
):
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(**{class_name: RecordingAuthProvider}),
    )
    monkeypatch.setenv(
        f"FASTMCP_SERVER_AUTH_{env_prefix}_REQUIRED_SCOPES",
        configured_scopes,
    )

    provider = _load_fastmcp_auth_provider(provider_path)

    assert provider.kwargs["required_scopes"] == default_scopes


@pytest.mark.parametrize(
    ("provider_path", "class_name", "env_prefix"),
    [
        (
            "fastmcp.server.auth.providers.discord.DiscordProvider",
            "DiscordProvider",
            "DISCORD",
        ),
        (
            "fastmcp.server.auth.providers.github.GitHubProvider",
            "GitHubProvider",
            "GITHUB",
        ),
        (
            "fastmcp.server.auth.providers.google.GoogleProvider",
            "GoogleProvider",
            "GOOGLE",
        ),
        (
            "fastmcp.server.auth.providers.workos.WorkOSProvider",
            "WorkOSProvider",
            "WORKOS",
        ),
    ],
)
def test_legacy_zero_timeout_restores_provider_default(
    monkeypatch,
    provider_path,
    class_name,
    env_prefix,
):
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(**{class_name: RecordingAuthProvider}),
    )
    monkeypatch.setenv(f"FASTMCP_SERVER_AUTH_{env_prefix}_TIMEOUT_SECONDS", "0")

    provider = _load_fastmcp_auth_provider(provider_path)

    assert provider.kwargs["timeout_seconds"] == 10


def test_legacy_introspection_keeps_zero_timeout(monkeypatch):
    provider_path = "fastmcp.server.auth.providers.introspection.IntrospectionTokenVerifier"
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(
            IntrospectionTokenVerifier=RecordingAuthProvider
        ),
    )
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_INTROSPECTION_INTROSPECTION_URL", "https://idp")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_INTROSPECTION_CLIENT_ID", "client")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_INTROSPECTION_CLIENT_SECRET", "secret")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_INTROSPECTION_TIMEOUT_SECONDS", "0")

    provider = _load_fastmcp_auth_provider(provider_path)

    assert provider.kwargs["timeout_seconds"] == 0


def test_legacy_empty_aws_region_restores_default(monkeypatch):
    provider_path = "fastmcp.server.auth.providers.aws.AWSCognitoProvider"
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(AWSCognitoProvider=RecordingAuthProvider),
    )
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_AWS_COGNITO_AWS_REGION", "")

    provider = _load_fastmcp_auth_provider(provider_path)

    assert provider.kwargs["aws_region"] == "eu-central-1"


@pytest.mark.parametrize("required_field", ["introspection_url", "client_id", "client_secret"])
def test_legacy_introspection_rejects_empty_required_fields_without_values(
    monkeypatch,
    required_field,
):
    provider_path = "fastmcp.server.auth.providers.introspection.IntrospectionTokenVerifier"
    values = {
        "introspection_url": "https://idp.example/introspect",
        "client_id": "client",
        "client_secret": "secret-value",
    }
    values[required_field] = ""
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(
            IntrospectionTokenVerifier=RecordingAuthProvider
        ),
    )
    for name, value in values.items():
        monkeypatch.setenv(f"FASTMCP_SERVER_AUTH_INTROSPECTION_{name.upper()}", value)

    with pytest.raises(ValueError) as exc_info:
        _load_fastmcp_auth_provider(provider_path)

    assert required_field.upper() in str(exc_info.value)
    assert "secret-value" not in str(exc_info.value)


def test_load_auth_provider_supports_no_arg_custom_provider(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(CustomProvider=RecordingAuthProvider),
    )

    provider = _load_fastmcp_auth_provider("example.auth.CustomProvider")

    assert provider.kwargs == {}


def test_load_auth_provider_rejects_invalid_json_without_secret_value(
    monkeypatch: pytest.MonkeyPatch,
):
    provider_path = "fastmcp.server.auth.providers.github.GitHubProvider"
    invalid_value = "secret-invalid-value"
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(GitHubProvider=RecordingAuthProvider),
    )
    monkeypatch.setenv(
        "FASTMCP_SERVER_AUTH_GITHUB_ALLOWED_CLIENT_REDIRECT_URIS",
        invalid_value,
    )

    with pytest.raises(ValueError) as exc_info:
        _load_fastmcp_auth_provider(provider_path)

    assert "FASTMCP_SERVER_AUTH_GITHUB_ALLOWED_CLIENT_REDIRECT_URIS" in str(exc_info.value)
    assert invalid_value not in str(exc_info.value)


def test_load_auth_provider_rejects_removed_supabase_hs256(
    monkeypatch: pytest.MonkeyPatch,
):
    provider_path = "fastmcp.server.auth.providers.supabase.SupabaseProvider"
    monkeypatch.setattr(
        mcp_server_module.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(SupabaseProvider=RecordingAuthProvider),
    )
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_SUPABASE_ALGORITHM", "HS256")

    with pytest.raises(ValueError, match="not supported by FastMCP 4"):
        _load_fastmcp_auth_provider(provider_path)


def test_resolve_auth_disabled_passes_explicit_none(monkeypatch: pytest.MonkeyPatch):
    """CLICKHOUSE_MCP_AUTH_DISABLED=true returns {"auth": None}, not {}."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_DISABLED", "true")

    assert _resolve_auth(MCPServerConfig()) == {"auth": None}


def test_resolve_auth_rejects_multiple_modes(monkeypatch: pytest.MonkeyPatch):
    """Configuring more than one auth mode raises ValueError."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")
    monkeypatch.setenv("CLICKHOUSE_MCP_AUTH_TOKEN", "secret")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH", "fastmcp.server.auth.providers.jwt.JWTVerifier")

    with pytest.raises(ValueError, match="mutually exclusive"):
        _resolve_auth(MCPServerConfig())


def test_resolve_auth_http_without_any_mode_raises(monkeypatch: pytest.MonkeyPatch):
    """HTTP transport with no auth configured raises ValueError."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "http")

    with pytest.raises(ValueError):
        _resolve_auth(MCPServerConfig())
