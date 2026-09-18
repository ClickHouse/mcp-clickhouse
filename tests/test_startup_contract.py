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


@pytest.mark.parametrize("entrypoint", ["package", "file", "package-file", "file-file"])
def test_entrypoint_keeps_independent_executor_and_chdb_ownership(isolated_package, entrypoint):
    source_root, _ = isolated_package
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "fastmcp.json", source_root / "fastmcp.json"
    )
    _run_python(
        isolated_package,
        """
        import asyncio
        import atexit
        import concurrent.futures
        import importlib
        import json
        import os
        import sys
        import types
        from pathlib import Path
        from unittest.mock import patch

        import clickhouse_connect
        from fastmcp import Client
        from fastmcp.utilities.mcp_server_config import MCPServerConfig
        from starlette.requests import Request

        pools, sessions, callbacks, events = [], [], [], []
        submissions, snapshots = [], []
        original_register = atexit.register

        class Pool(concurrent.futures.ThreadPoolExecutor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                if args or "max_workers" in kwargs:
                    pools.append(self)

            def submit(self, fn, /, *args, **kwargs):
                if self in pools:
                    submissions.append((pools.index(self), fn.__self__, args))
                return super().submit(fn, *args, **kwargs)

            def shutdown(self, wait=True, **kwargs):
                if self in pools:
                    events.append(("pool", pools.index(self), wait))
                super().shutdown(wait=wait, **kwargs)

        class Result:
            def __init__(self, session, query):
                self.result = [{"session": session, "query": query}]

            def has_error(self):
                return False

            def data(self):
                return json.dumps({"data": self.result})

        class Session:
            def __init__(self, path):
                sessions.append(self)

            def query(self, query, output_format):
                assert output_format == "JSON"
                return Result(sessions.index(self), query)

            def close(self):
                events.append(("chdb", sessions.index(self)))

        def record_register(callback, *args, **kwargs):
            if callback.__name__ == "_shutdown" or isinstance(
                getattr(callback, "__self__", None), Session
            ):
                callbacks.append(callback)
                assert not args and not kwargs
                return callback
            return original_register(callback, *args, **kwargs)

        chdb = types.ModuleType("chdb")
        chdb.__path__ = []
        chdb.session = types.ModuleType("chdb.session")
        chdb.session.Session = Session
        sys.modules.update({"chdb": chdb, "chdb.session": chdb.session})

        async def check_async_calls(namespace, backend, index):
            result = await namespace["run_chdb_select_query_async"]("async")
            assert json.loads(result) == [{"session": index, "query": "async"}]
            assert submissions[-1] == (index * 4, backend, ("async",))
            async with Client(namespace["mcp"]) as client:
                assert {tool.name for tool in await client.list_tools()} == {"run_chdb_select_query"}
                assert {prompt.name for prompt in await client.list_prompts()} == {"chdb_initial_prompt"}
                result = await client.call_tool("run_chdb_select_query", {"query": "mcp"})
                assert json.loads(result.content[0].text) == [{"session": index, "query": "mcp"}]
                assert submissions[-1] == (index * 4, backend, ("mcp",))

        async def check_health_ownership():
            request = Request({"type": "http", "method": "GET", "headers": []})
            for failed_index, (namespace, backend, *_) in enumerate(snapshots):
                backend.client = None
                backend.error_message = "private initialization detail"
                try:
                    for index, (other, other_backend, *_) in enumerate(snapshots):
                        assert other["_health"].chdb_backend is other_backend
                        assert other["health_check"].__self__ is other["_health"]
                        response = await other["health_check"](request)
                        if index == failed_index:
                            assert response.status_code == 503
                            assert response.body == (
                                b"ERROR. chDB initialization failed. Check server logs for details."
                            )
                        else:
                            assert response.status_code == 200 and response.body == b"OK"
                    backend.error_message = None
                    response = await namespace["health_check"](request)
                    assert response.status_code == 503
                    assert b"Server misconfigured" in response.body
                finally:
                    backend.client = sessions[failed_index]
                    backend.error_message = None

        def check_assemblies():
            namespaces = [callback.__globals__ for callback in callbacks[::2]]
            for namespace in namespaces[len(snapshots):]:
                snapshots.append((
                    namespace, namespace["_chdb_backend"], namespace["create_chdb_client"],
                    namespace["run_chdb_select_query"], namespace["run_chdb_select_query_async"],
                ))
            assert len({id(snapshot[1]) for snapshot in snapshots}) == len(namespaces)
            for index, (namespace, backend, create, run_sync, run_async) in enumerate(snapshots):
                assert namespace["_chdb_backend"] is backend
                assert namespace["create_chdb_client"] is create
                assert namespace["run_chdb_select_query"] is run_sync
                assert namespace["run_chdb_select_query_async"] is run_async
                assert create.__self__ is run_sync.__self__ is run_async.__self__ is backend
                assert backend.executors is namespace["_executors"]
                assert create() is backend.client is sessions[index]
                assert backend.error_message is None
                assert json.loads(run_sync("sync")) == [{"session": index, "query": "sync"}]
                assert submissions[-1] == (index * 4, backend, ("sync",))
                asyncio.run(check_async_calls(namespace, backend, index))
            asyncio.run(check_health_ownership())
            package = sys.modules["mcp_clickhouse"]
            for name in ("create_chdb_client", "run_chdb_select_query", "chdb_initial_prompt"):
                assert getattr(package, name) is namespaces[0][name]
            assert not events

        with patch("concurrent.futures.ThreadPoolExecutor", Pool), patch(
            "atexit.register", record_register
        ), patch.object(
            clickhouse_connect, "get_client", side_effect=AssertionError("Unexpected DB connection")
        ) as get_client:
            assert asyncio.run(asyncio.to_thread(lambda: "default executor")) == "default executor"
            assert not pools and not events
            entrypoint = os.environ["STARTUP_ENTRYPOINT"]
            for step in entrypoint.split("-"):
                if step == "package":
                    loaded = importlib.import_module("mcp_clickhouse.mcp_server").mcp
                else:
                    os.chdir(os.environ["PYTHONPATH"])
                    config = MCPServerConfig.from_file(Path(os.environ["PYTHONPATH"]) / "fastmcp.json")
                    loaded = asyncio.run(config.source.load_server())
                check_assemblies()
            count = {"package": 1, "file": 2, "package-file": 2, "file-file": 3}[entrypoint]
            get_client.assert_not_called()

        shutdowns = callbacks[::2]
        namespaces = [callback.__globals__ for callback in shutdowns]
        assert [callback.__name__ for callback in callbacks] == ["_shutdown", "close"] * count
        assert namespaces[0] is sys.modules["mcp_clickhouse.mcp_server"].__dict__
        assert namespaces[-1]["mcp"] is loaded
        assert len({id(namespace["mcp"]) for namespace in namespaces}) == count
        assert len({id(pool) for pool in pools}) == 4 * count
        assert len(sessions) == count
        assert [pool._max_workers for pool in pools] == [6, 4, 2, 1] * count
        for index, namespace in enumerate(namespaces):
            executors = namespace["_executors"]
            assert executors.max_workers == 6
            assert [executors.query, executors.metadata, executors.cancellation, executors.health] == (
                pools[index * 4:(index + 1) * 4]
            )
            assert namespace["_chdb_backend"].client is sessions[index]
            namespace["_clickhouse_clients"]._clear_client_cache = (
                lambda index=index: events.append(("cache", index))
            )

        # Preserve the existing atexit order during the structural refactor.
        expected = []
        for index in reversed(range(count)):
            expected.append(("chdb", index))
            expected.extend(("pool", pool, True) for pool in range(index * 4, (index + 1) * 4))
            expected.append(("cache", index))
        for callback in reversed(callbacks):
            callback()
        assert events == expected, events
        """,
        {"STARTUP_ENTRYPOINT": entrypoint, "CHDB_ENABLED": "true", "CLICKHOUSE_MCP_MAX_WORKERS": "6"},
    )


@pytest.mark.parametrize("entrypoint", ["package", "file", "package-file", "file-file"])
def test_entrypoint_keeps_independent_clickhouse_owners(isolated_package, entrypoint):
    source_root, _ = isolated_package
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "fastmcp.json", source_root / "fastmcp.json"
    )
    _run_python(
        isolated_package,
        """
        import asyncio
        import atexit
        import concurrent.futures
        import importlib
        import json
        import os
        import sys
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        import clickhouse_connect
        from fastmcp import Client
        from fastmcp.utilities.mcp_server_config import MCPServerConfig
        from starlette.testclient import TestClient

        health_snapshots = []
        callbacks, created, snapshots, query_snapshots, metadata_snapshots = [], [], [], [], []
        shared_page_token = "52c27074-a18d-4b1f-870b-1454dfc6bf8c"
        original_register = atexit.register

        class Connection:
            server_version = "test"
            server_settings = {}

            def __init__(self, **config):
                self.config = config
                self.index = len(created)
                self.closed = False
                self.grants = 0
                self.commands = []
                created.append(self)

            def query(self, query, **kwargs):
                assert not self.closed
                if query == "SHOW GRANTS":
                    self.grants += 1
                    return SimpleNamespace(result_rows=[])
                return SimpleNamespace(column_names=["client"], result_rows=[[self.index]])

            def command(self, query):
                assert not self.closed
                self.commands.append(query)
                if query == "SHOW DATABASES":
                    return f"database_{self.index}\\nshared"
                return "default"

            def close(self):
                assert not self.closed
                self.closed = True

        def record_register(callback, *args, **kwargs):
            if callback.__name__ == "_shutdown":
                callbacks.append(callback)
                assert not args and not kwargs
                return callback
            return original_register(callback, *args, **kwargs)

        async def check_tool(namespace, expected):
            async with Client(namespace["mcp"]) as client:
                result = await client.call_tool("run_query", {"query": "SELECT 1"})
                assert json.loads(result.content[0].text)["rows"] == [[expected.index]]

        async def call_metadata_tool(namespace, name, arguments):
            async with Client(namespace["mcp"]) as client:
                result = await client.call_tool(name, arguments)
                return result.content[0].text

        def seed_page(owner, create, names):
            with patch("mcp_clickhouse.metadata.uuid.uuid4", return_value=shared_page_token):
                assert create("database", None, None, names, 1, True) == shared_page_token
            assert owner.table_pagination_cache[shared_page_token]["table_names"] == names

        def check_metadata(index, namespace, manager, cached, config):
            owner, cache, lock, saved = metadata_snapshots[index]
            assert namespace["_metadata"] is owner
            assert owner.clients is manager
            assert owner.executors is namespace["_executors"]
            assert namespace["table_pagination_cache"] is owner.table_pagination_cache is cache
            assert owner.table_pagination_cache_lock is lock
            assert cache.maxsize == 100 and cache.ttl == 3600
            for name, function in saved.items():
                assert namespace[name] is function
                assert function.__self__ is owner
            assert asyncio.iscoroutinefunction(saved["list_databases_async"])
            assert asyncio.iscoroutinefunction(saved["list_tables_async"])
            names = [f"{cached.index}_first", f"{cached.index}_second", f"{cached.index}_third"]
            assert cache[shared_page_token]["table_names"] == names
            with patch.object(
                owner.executors.metadata, "submit", wraps=owner.executors.metadata.submit,
            ) as submit:
                expected = [f"database_{cached.index}", "shared"]
                assert json.loads(saved["list_databases"]()) == expected
                assert json.loads(asyncio.run(saved["list_databases_async"]())) == expected
                assert json.loads(asyncio.run(call_metadata_tool(namespace, "list_databases", {}))) == expected
            assert submit.call_count == 2
            for call in submit.call_args_list:
                assert call.args == (owner._list_databases_with_config, config)
            for mode in ("sync", "async", "mcp"):
                before = [dict(snapshot[1]) for snapshot in metadata_snapshots]
                with patch.object(
                    owner.executors.metadata, "submit", wraps=owner.executors.metadata.submit,
                ) as submit, patch.object(
                    owner.clients, "_acquire_clickhouse_client",
                    wraps=owner.clients._acquire_clickhouse_client,
                ) as acquire, patch.object(
                    owner.clients, "_release_client_entry",
                    wraps=owner.clients._release_client_entry,
                ) as release, patch(
                    "mcp_clickhouse.metadata.fetch_table_names_from_system",
                    return_value=names,
                ) as fetch, patch(
                    "mcp_clickhouse.metadata.get_paginated_table_data",
                    return_value=([], 2, True),
                ) as page:
                    arguments = {"database": "database", "page_token": shared_page_token, "page_size": 1}
                    if mode == "sync":
                        result = saved["list_tables"](**arguments)
                        submit.assert_not_called()
                    elif mode == "async":
                        result = asyncio.run(saved["list_tables_async"](**arguments))
                    else:
                        result = asyncio.run(call_metadata_tool(namespace, "list_tables", arguments))
                    if mode != "sync":
                        submit.assert_called_once_with(
                            owner._list_tables_with_config, config, "database", None, None,
                            shared_page_token, 1, True, before[index][shared_page_token],
                        )
                    result = json.loads(result)
                    acquire.assert_called_once_with(config)
                    release.assert_called_once_with(next(iter(manager.cache.values())))
                    fetch.assert_not_called()
                    page.assert_called_once_with(cached, "database", names, 1, 1, True)
                next_token = result["next_page_token"]
                assert result == {"tables": [], "total_tables": 3, "next_page_token": next_token}
                assert shared_page_token not in cache
                assert list(cache) == [next_token]
                assert cache[next_token]["start_idx"] == 2
                for other_index, snapshot in enumerate(metadata_snapshots):
                    if other_index != index:
                        assert dict(snapshot[1]) == before[other_index]
                with lock:
                    cache.clear()
                seed_page(owner, saved["create_page_token"], names)

        def check_health(index, namespace, manager, cached, config):
            owner, lock, logged, handler, app = health_snapshots[index]
            assert namespace["_health"] is owner
            assert owner.clients is manager
            assert owner.executors is namespace["_executors"]
            assert owner.chdb_backend is namespace["_chdb_backend"]
            assert owner.health_probe_lock is lock
            assert owner.logged_health_probe_futures is logged
            assert namespace["health_check"] is handler
            assert handler.__self__ is owner and asyncio.iscoroutinefunction(handler)
            route = next(route for route in app.routes if route.path == "/health")
            assert route.endpoint is handler
            assert route.methods == {"GET", "HEAD"}
            owner._clear_health_result_cache()
            with patch.object(
                owner.executors.health, "submit", wraps=owner.executors.health.submit,
            ) as submit, patch.object(
                owner.clients, "_acquire_clickhouse_client",
                wraps=owner.clients._acquire_clickhouse_client,
            ) as acquire, patch.object(
                owner.clients, "_release_client_entry",
                wraps=owner.clients._release_client_entry,
            ) as release, TestClient(app, base_url="http://untrusted.example") as client:
                response = client.get("/health", headers={"origin": "https://untrusted.example"})
                assert response.status_code == 200 and response.content == b"OK"
            submit.assert_called_once_with(owner._probe_clickhouse_health, config)
            acquire.assert_called_once_with(config)
            release.assert_called_once_with(next(iter(manager.cache.values())))
            assert cached.commands[-1] == "SELECT 1"
            assert owner.health_probe_future is None
            assert owner._cached_health_result() is True

        def check_health_state_isolation():
            futures = [concurrent.futures.Future() for _ in health_snapshots]
            shared_log_future = concurrent.futures.Future()
            for (owner, *_), future in zip(health_snapshots, futures):
                owner._clear_health_result_cache()
                with patch.object(owner.executors.health, "submit", return_value=future) as submit:
                    config = {"connect_timeout": 2.0, "send_receive_timeout": 2.0}
                    assert owner._get_health_probe_future(config) is future
                    assert owner._get_health_probe_future(config) is future
                submit.assert_called_once_with(owner._probe_clickhouse_health, config)
                assert owner._claim_health_probe_log(shared_log_future) is True
                assert owner._claim_health_probe_log(shared_log_future) is False
            for index, future in enumerate(futures):
                before = [snapshot[0].health_result_cache for snapshot in health_snapshots]
                future.set_result(None)
                for other_index, (owner, *_) in enumerate(health_snapshots):
                    if other_index == index:
                        assert owner.health_probe_future is None
                        assert owner._cached_health_result() is True
                    else:
                        assert owner.health_result_cache == before[other_index]
                        if other_index > index:
                            assert owner.health_probe_future is futures[other_index]
            for failed_index in range(len(health_snapshots)):
                for index, (owner, *_) in enumerate(health_snapshots):
                    with owner.health_probe_lock:
                        owner.health_result_cache = (float("inf"), index != failed_index)
                for index, (owner, _, _, _, app) in enumerate(health_snapshots):
                    with patch.object(owner.executors.health, "submit") as submit, TestClient(
                        app, base_url="http://untrusted.example",
                    ) as client:
                        response = client.get("/health")
                        head = client.head("/health")
                    assert response.status_code == head.status_code == (
                        503 if index == failed_index else 200
                    )
                    expected = (
                        b"ERROR. ClickHouse connection failed. Check server logs for details."
                        if index == failed_index else b"OK"
                    )
                    assert response.content == expected and head.content == b""
                    submit.assert_not_called()
            for owner, *_ in health_snapshots:
                owner._clear_health_result_cache()
                owner.logged_health_probe_futures.clear()

        def check_cancellation():
            query_id = "fd0d52e3-0afc-4e38-8c7d-b0bd0de37f66"
            states = []
            for owner, *_ in query_snapshots:
                state = owner._register_active_query(query_id, "SELECT 1")
                with owner.active_queries_lock:
                    state.client_entry = next(iter(owner.clients.cache.values()))
                states.append(state)
            assert len({id(state) for state in states}) == len(states)
            try:
                for cancel_index in (3, 4):
                    for index, (owner, *_) in enumerate(query_snapshots):
                        for other, *_ in query_snapshots:
                            with other.active_queries_lock:
                                other.active_queries[query_id].cancelled = False
                        entry = states[index].client_entry
                        before = [len(state.client_entry.client.commands) for state in states]
                        with patch.object(
                            owner.executors.cancellation, "submit",
                            wraps=owner.executors.cancellation.submit,
                        ) as submit, patch.object(
                            owner.clients, "_retain_client_entry",
                            wraps=owner.clients._retain_client_entry,
                        ) as retain, patch.object(
                            owner.clients, "_release_client_entry",
                            wraps=owner.clients._release_client_entry,
                        ) as release:
                            result = query_snapshots[index][cancel_index](query_id)
                            if asyncio.iscoroutine(result):
                                asyncio.run(result)
                        submit.assert_called_once_with(owner._cancel_query, query_id)
                        retain.assert_called_once_with(entry)
                        release.assert_called_once_with(entry)
                        assert entry.active_users == 0
                        assert entry.client.commands[-1] == (
                            f"KILL QUERY WHERE query_id = '{query_id}'"
                        )
                        for other_index, state in enumerate(states):
                            assert state.cancelled is (other_index == index)
                            assert len(state.client_entry.client.commands) == (
                                before[other_index] + int(other_index == index)
                            )
            finally:
                for (owner, *_), state in zip(query_snapshots, states):
                    owner._remove_active_query(query_id, state)

        with patch("atexit.register", record_register), patch.object(
            clickhouse_connect, "get_client", side_effect=Connection
        ):
            for step in os.environ["STARTUP_ENTRYPOINT"].split("-"):
                before_load = len(created)
                if step == "package":
                    importlib.import_module("mcp_clickhouse.mcp_server")
                else:
                    os.chdir(os.environ["PYTHONPATH"])
                    config = MCPServerConfig.from_file(Path(os.environ["PYTHONPATH"]) / "fastmcp.json")
                    asyncio.run(config.source.load_server())
                assert len(created) == before_load
                namespaces = [callback.__globals__ for callback in callbacks]
                for namespace in namespaces[len(snapshots):]:
                    manager = namespace["_clickhouse_clients"]
                    assert not manager.cache and manager.grants_advisory_done is False
                    create = namespace["create_clickhouse_client"]
                    independent = create()
                    assert independent.grants == 1
                    assert manager.grants_advisory_done is True
                    independent.close()
                    config = namespace["clients"]._resolve_client_config()
                    entry = manager._acquire_clickhouse_client(config)
                    manager._release_client_entry(entry)
                    assert entry.client.config["autogenerate_session_id"] is False
                    snapshots.append((namespace, manager, entry.client, create, config))
                    owner = namespace["_queries"]
                    assert not owner.active_queries
                    query_snapshots.append((
                        owner, namespace["run_query"], namespace["run_query_async"],
                        owner._cancel_query_with_bounded_wait, owner._cancel_query_async,
                    ))
                    metadata = namespace["_metadata"]
                    assert not metadata.table_pagination_cache
                    saved = {name: namespace[name] for name in (
                        "list_databases", "list_databases_async", "list_tables",
                        "list_tables_async", "create_page_token",
                    )}
                    metadata_snapshots.append((
                        metadata, metadata.table_pagination_cache,
                        metadata.table_pagination_cache_lock, saved,
                    ))
                    health = namespace["_health"]
                    assert health.health_probe_future is None and health.health_result_cache is None
                    assert not health.logged_health_probe_futures
                    health_snapshots.append((
                        health, health.health_probe_lock, health.logged_health_probe_futures,
                        namespace["health_check"], namespace["mcp"].http_app(),
                    ))
                    seed_page(metadata, saved["create_page_token"], [
                        f"{entry.client.index}_first", f"{entry.client.index}_second",
                        f"{entry.client.index}_third",
                    ])
                assert len({id(snapshot[1]) for snapshot in snapshots}) == len(snapshots)
                assert len({id(snapshot[1].cache) for snapshot in snapshots}) == len(snapshots)
                assert len({id(snapshot[1].lock) for snapshot in snapshots}) == len(snapshots)
                assert len({id(snapshot[0]) for snapshot in query_snapshots}) == len(snapshots)
                assert len({id(snapshot[0].active_queries) for snapshot in query_snapshots}) == len(snapshots)
                assert len({id(snapshot[0].active_queries_lock) for snapshot in query_snapshots}) == len(snapshots)
                for part in (0, 1, 2):
                    assert len({id(snapshot[part]) for snapshot in metadata_snapshots}) == len(snapshots)
                    assert len({id(snapshot[part]) for snapshot in health_snapshots}) == len(snapshots)
                for index, (namespace, manager, cached, create, config) in enumerate(snapshots):
                    assert namespace["_clickhouse_clients"] is manager
                    assert namespace["create_clickhouse_client"] is create
                    assert create.__self__ is manager
                    owner, run_sync, run_async, *_ = query_snapshots[index]
                    assert namespace["_queries"] is owner
                    assert namespace["run_query"] is run_sync
                    assert namespace["run_query_async"] is run_async
                    assert run_sync.__self__ is run_async.__self__ is owner
                    assert owner.clients is manager
                    assert owner.executors is namespace["_executors"]
                    independent = create()
                    assert independent is not cached and independent.grants == 0
                    assert "autogenerate_session_id" not in independent.config
                    independent.close()
                    with patch.object(
                        owner.executors.query, "submit", wraps=owner.executors.query.submit,
                    ) as submit:
                        assert json.loads(run_sync("SELECT 1"))["rows"] == [[cached.index]]
                        result = asyncio.run(run_async("SELECT 1"))
                        assert json.loads(result)["rows"] == [[cached.index]]
                        asyncio.run(check_tool(namespace, cached))
                    assert submit.call_count == 3
                    for call in submit.call_args_list:
                        assert call.args[0].__self__ is owner
                        assert call.args[0] == owner.execute_query
                        assert call.args[1] == "SELECT 1"
                        assert call.args[3] == config and call.args[4] is None
                    assert not owner.active_queries
                    check_metadata(index, namespace, manager, cached, config)
                    check_health(index, namespace, manager, cached, config)
                    assert len(manager.cache) == 1
                    assert next(iter(manager.cache.values())).client is cached
                assert sys.modules["mcp_clickhouse"].create_clickhouse_client is snapshots[0][3]
                assert sys.modules["mcp_clickhouse"].run_query is query_snapshots[0][1]
                package = sys.modules["mcp_clickhouse"]
                assert package.table_pagination_cache is metadata_snapshots[0][1]
                for name in ("list_databases", "list_tables", "create_page_token"):
                    assert getattr(package, name) is metadata_snapshots[0][3][name]
                check_cancellation()
                check_health_state_isolation()

        count = {"package": 1, "file": 2, "package-file": 2, "file-file": 3}[
            os.environ["STARTUP_ENTRYPOINT"]
        ]
        assert len(snapshots) == count
        for index, callback in reversed(list(enumerate(callbacks))):
            callback()
            for other, (_, manager, cached, *_) in enumerate(snapshots):
                assert cached.closed is (other >= index)
                assert bool(manager.cache) is (other < index)
        """,
        {
            "STARTUP_ENTRYPOINT": entrypoint,
            "CLICKHOUSE_ENABLED": "true",
            "CLICKHOUSE_HOST": "localhost",
            "CLICKHOUSE_USER": "test",
            "CLICKHOUSE_PASSWORD": "",
            "CLICKHOUSE_ALLOW_WRITE_ACCESS": "true",
            "CLICKHOUSE_CONNECT_TIMEOUT": "2",
            "CLICKHOUSE_SEND_RECEIVE_TIMEOUT": "2",
            "CLICKHOUSE_MCP_AUTH_TOKEN": "startup-health-token",
        },
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
