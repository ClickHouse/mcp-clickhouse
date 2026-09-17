import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def isolated_package(tmp_path):
    source_root = tmp_path / "source"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "mcp_clickhouse",
        source_root / "mcp_clickhouse",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (source_root / ".env").write_text("")
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    return source_root, launch_dir


def _run_python(isolated_package, script, env=None):
    source_root, launch_dir = isolated_package
    child_env = {
        name: value
        for name, value in os.environ.items()
        if not name.casefold().startswith(
            ("clickhouse_", "chdb_", "fastmcp_", "mcp_", "python_dotenv")
        )
    }
    child_env.update(
        {
            "PYTHONPATH": str(source_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "MCP_CLICKHOUSE_TRUSTSTORE_DISABLE": "1",
            "CLICKHOUSE_ENABLED": "false",
            "CHDB_ENABLED": "false",
        }
    )
    child_env.update(env or {})
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=launch_dir,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "clickhouse_enabled,chdb_enabled,chdb_available",
    [
        (True, False, False),
        (False, True, True),
        (True, True, True),
        (False, False, False),
        (True, True, False),
    ],
    ids=["clickhouse", "chdb", "both", "neither", "chdb-unavailable"],
)
def test_fresh_submodule_import_preserves_registration_and_exports(
    isolated_package, clickhouse_enabled, chdb_enabled, chdb_available
):
    _run_python(
        isolated_package,
        """
        import asyncio
        import importlib
        import importlib.abc
        import importlib.util
        import os
        import sys
        from unittest.mock import patch

        import clickhouse_connect
        from fastmcp import Client

        clickhouse_enabled = os.environ["CLICKHOUSE_ENABLED"] == "true"
        chdb_enabled = os.environ["CHDB_ENABLED"] == "true"
        chdb_available = os.environ["STARTUP_CHDB_AVAILABLE"] == "true"
        import_attempts = []
        session_paths = []

        class Session:
            def __init__(self, path):
                session_paths.append(path)

            def close(self):
                pass

        class ChDBImport(importlib.abc.MetaPathFinder, importlib.abc.Loader):
            def find_spec(self, fullname, path=None, target=None):
                if fullname != "chdb" and not fullname.startswith("chdb."):
                    return None
                import_attempts.append(fullname)
                if not chdb_available:
                    raise ModuleNotFoundError("No module named 'chdb'", name="chdb")
                return importlib.util.spec_from_loader(
                    fullname, self, is_package=fullname == "chdb"
                )

            def create_module(self, spec):
                return None

            def exec_module(self, module):
                if module.__name__ == "chdb.session":
                    module.Session = Session

        sys.meta_path.insert(0, ChDBImport())
        assert "mcp_clickhouse" not in sys.modules
        with patch.object(
            clickhouse_connect, "get_client", side_effect=AssertionError("Unexpected DB connection")
        ) as get_client:
            serialization = importlib.import_module("mcp_clickhouse.serialization")
            assert "mcp_clickhouse.mcp_server" in sys.modules
            package = sys.modules["mcp_clickhouse"]
            server = sys.modules["mcp_clickhouse.mcp_server"]
            assert package.serialization is serialization
            assert package.mcp_server is server

            exports = {
                "list_databases", "list_tables", "run_query", "create_clickhouse_client",
                "create_chdb_client", "run_chdb_select_query", "chdb_initial_prompt",
                "table_pagination_cache", "fetch_table_names_from_system",
                "get_paginated_table_data", "create_page_token",
            }
            assert set(package.__all__) == exports
            for name in exports:
                assert getattr(package, name) is getattr(server, name), name
            token = package.create_page_token("startup", None, None, ["first", "second"], 1, True)
            assert token in package.table_pagination_cache
            assert server.CLIENT_CONFIG_OVERRIDES_KEY == "clickhouse_client_config_overrides"
            transport = importlib.import_module("mcp_clickhouse.transport")
            assert server.ClickHouseFastMCP is transport.ClickHouseFastMCP
            assert isinstance(server.mcp, server.ClickHouseFastMCP)
            try:
                with patch("mcp_clickhouse.mcp_server._resolve_auth"):
                    raise AssertionError("Obsolete auth patch still resolves")
            except AttributeError:
                pass
            assert importlib.import_module("mcp_clickhouse.main").mcp is server.mcp
            for name in ("http_app", "sse_app", "streamable_http_app", "run_http_async"):
                assert callable(getattr(server.mcp, name)), name

            expected_tools = {"list_databases", "list_tables", "run_query"} if clickhouse_enabled else set()
            expected_prompts = set()
            if chdb_enabled and chdb_available:
                expected_tools.add("run_chdb_select_query")
                expected_prompts.add("chdb_initial_prompt")

            async def check_registration():
                async with Client(server.mcp) as client:
                    assert {tool.name for tool in await client.list_tools()} == expected_tools
                    assert {prompt.name for prompt in await client.list_prompts()} == expected_prompts

            asyncio.run(check_registration())
            os.environ["CLICKHOUSE_ENABLED"] = str(not clickhouse_enabled).lower()
            os.environ["CHDB_ENABLED"] = str(not chdb_enabled).lower()
            asyncio.run(check_registration())
            get_client.assert_not_called()

        if not chdb_enabled:
            assert import_attempts == []
        elif chdb_available:
            assert "chdb.session" in import_attempts
        else:
            assert import_attempts
        assert session_paths == ([":memory:"] if chdb_enabled and chdb_available else [])
        """,
        {
            "CLICKHOUSE_ENABLED": str(clickhouse_enabled).lower(),
            "CHDB_ENABLED": str(chdb_enabled).lower(),
            "STARTUP_CHDB_AVAILABLE": str(chdb_available).lower(),
        },
    )


def test_selected_dotenv_configures_initial_executors_and_http_auth(isolated_package):
    source_root, _ = isolated_package
    (source_root / ".env").write_text(
        "CLICKHOUSE_MCP_MAX_WORKERS=3\n"
        "CLICKHOUSE_MCP_SERVER_TRANSPORT=http\n"
        "CLICKHOUSE_MCP_AUTH_TOKEN=dotenv-test-token\n"
        "CLICKHOUSE_MCP_ALLOWED_HOSTS=localhost\n"
    )
    _run_python(
        isolated_package,
        """
        import concurrent.futures
        import importlib
        import os
        import sys
        from unittest.mock import call, patch

        # Keep dependency startup outside the startup spies.
        import clickhouse_connect
        import fastmcp
        from pydantic import TypeAdapter
        from starlette.testclient import TestClient

        parser_startup_state = []
        init_adapter = TypeAdapter.__init__
        def record_adapter(adapter, adapted_type, *args, _parent_depth=2, **kwargs):
            if adapted_type is int:
                parser_startup_state.append((
                    os.environ.get("CLICKHOUSE_MCP_MAX_WORKERS"), executor.call_count
                ))
            init_adapter(
                adapter, adapted_type, *args, _parent_depth=_parent_depth + 1, **kwargs
            )

        assert "mcp_clickhouse" not in sys.modules
        with patch(
            "concurrent.futures.ThreadPoolExecutor", wraps=concurrent.futures.ThreadPoolExecutor
        ) as executor, patch.object(TypeAdapter, "__init__", record_adapter):
            server = importlib.import_module("mcp_clickhouse.mcp_server")
        assert parser_startup_state == [("3", 4)], parser_startup_state
        # Preserve pool count and order during the structural refactor.
        # This can be relaxed after the refactor is complete.
        assert executor.call_args_list == [
            call(max_workers=3), call(max_workers=3), call(max_workers=2), call(max_workers=1)
        ]

        app = server.mcp.http_app()
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "startup-test", "version": "1"},
            },
        }
        headers = {"accept": "application/json, text/event-stream"}
        with TestClient(app, base_url="http://localhost") as client:
            for token in (None, "wrong-token"):
                auth_headers = {} if token is None else {"authorization": f"Bearer {token}"}
                response = client.post("/mcp", json=request, headers={**headers, **auth_headers})
                assert response.status_code == 401
                assert "dotenv-test-token" not in response.text
            response = client.post(
                "/mcp", json=request,
                headers={**headers, "authorization": "Bearer dotenv-test-token"},
            )
            assert response.status_code == 200, response.text
            assert '"serverInfo"' in response.text
        """,
    )


@pytest.mark.parametrize("app_method", ["http_app", "sse_app"])
def test_auth_provider_resolves_when_transport_app_is_constructed(isolated_package, app_method):
    source_root, _ = isolated_package
    (source_root / "startup_auth_provider.py").write_text(
        "from fastmcp.server.auth.providers.jwt import StaticTokenVerifier\n"
        "constructions = []\n"
        "class Provider(StaticTokenVerifier):\n"
        "    def __init__(self):\n"
        "        constructions.append(self)\n"
        "        super().__init__(tokens={}, required_scopes=[])\n"
    )
    _run_python(
        isolated_package,
        """
        import importlib
        import os
        import sys

        importlib.import_module("mcp_clickhouse.auth")
        server = sys.modules["mcp_clickhouse.mcp_server"]
        assert "startup_auth_provider" not in sys.modules
        assert server.mcp.auth is None

        os.environ["FASTMCP_SERVER_AUTH"] = "startup_auth_provider.Provider"
        getattr(server.mcp, os.environ["STARTUP_APP_METHOD"])()

        provider = sys.modules["startup_auth_provider"]
        assert len(provider.constructions) == 1
        assert server.mcp.auth is None
        """,
        {
            "STARTUP_APP_METHOD": app_method,
            "CLICKHOUSE_MCP_SERVER_TRANSPORT": "http" if app_method == "http_app" else "sse",
            "FASTMCP_SERVER_AUTH": "startup_auth_provider.UnavailableDuringImport",
        },
    )


@pytest.mark.parametrize(
    "entrypoint,transport",
    [("console", "stdio"), ("module", "http"), ("console", "sse")],
)
def test_cli_loads_middleware_before_running_server(isolated_package, entrypoint, transport):
    source_root, _ = isolated_package
    (source_root / "startup_middleware.py").write_text(
        "events = []\n"
        "def setup_middleware(mcp):\n"
        "    events.append(('middleware', mcp))\n"
    )
    _run_python(
        isolated_package,
        """
        import importlib.metadata
        import os
        import runpy
        from unittest.mock import patch

        import startup_middleware
        from mcp_clickhouse.mcp_server import mcp

        events = startup_middleware.events
        assert events == []
        def run(**kwargs):
            events.append(("run", mcp, kwargs))

        with patch.object(mcp, "run", side_effect=run) as run_server:
            if os.environ["STARTUP_ENTRYPOINT"] == "module":
                runpy.run_module("mcp_clickhouse.main", run_name="__main__")
            else:
                entrypoints = [
                    entry for entry in importlib.metadata.distribution("mcp-clickhouse").entry_points
                    if entry.group == "console_scripts" and entry.name == "mcp-clickhouse"
                ]
                assert len(entrypoints) == 1
                assert entrypoints[0].value == "mcp_clickhouse.main:main"
                main = entrypoints[0].load()
                assert events == []
                main()
            expected_kwargs = {"transport": os.environ["CLICKHOUSE_MCP_SERVER_TRANSPORT"]}
            if expected_kwargs["transport"] != "stdio":
                expected_kwargs.update(host="127.0.0.2", port=8124)
            run_server.assert_called_once_with(**expected_kwargs)
        assert events == [("middleware", mcp), ("run", mcp, expected_kwargs)]
        """,
        {
            "STARTUP_ENTRYPOINT": entrypoint,
            "CLICKHOUSE_MCP_SERVER_TRANSPORT": transport,
            "CLICKHOUSE_MCP_BIND_HOST": "127.0.0.2",
            "CLICKHOUSE_MCP_BIND_PORT": "8124",
            "MCP_MIDDLEWARE_MODULE": "startup_middleware",
        },
    )
