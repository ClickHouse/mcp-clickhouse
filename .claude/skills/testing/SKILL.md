---
name: testing
description: Use when running, writing, debugging, or modifying tests in mcp-clickhouse. Covers exact uv commands, ClickHouse and chDB test setup, fixture patterns, and ad hoc validation expectations.
---

# Testing skill

Tests live in `tests/`. Most expect a live ClickHouse on `localhost:8123`. Use the exact commands below rather than reaching for memory; the project standardizes on `uv` and `pytest`.

## Bring up the local ClickHouse

The compose file uses `CLICKHOUSE_USER=default`, `CLICKHOUSE_PASSWORD=clickhouse`, non-secure HTTP on port `8123`.

```bash
docker compose -f test-services/docker-compose.yaml up -d
```

CI runs against `clickhouse/clickhouse-server:24.10` with the `default` user and an empty password. Match the environment you are in rather than hardcoding credentials.

If a local server is required and is not available, say so to the user. Do not silently mock around it.

## Sync dependencies

```bash
uv sync --all-extras --dev
```

Use `uv pip install <pkg>` if you need a single package quickly.

## Run the suite

Full run:

```bash
uv run pytest tests
```

Targeted file or test:

```bash
uv run pytest tests/test_pagination.py
uv run pytest tests/test_mcp_server.py::test_run_query
```

Verbose, with stdout pass-through:

```bash
uv run pytest -v -s tests/test_mcp_server.py
```

## chDB tests

chDB lives behind the `chdb` optional extra. Tests that touch chDB need the extra installed and `CHDB_ENABLED=true`:

```bash
CHDB_ENABLED=true uv run --extra chdb pytest -v tests/test_chdb_tool.py
```

`tests/test_optional_chdb.py` exercises the path where the `chdb` package is not installed. The server must warn and skip chDB tool registration in that case, not crash. Keep that guarantee intact.

## Lint and format

```bash
uv run ruff check
uv run ruff format
```

`pyproject.toml` configures `line-length = 100`.

## Test layout reference

- `test_tool.py`, `test_mcp_server.py`, `test_pagination.py` exercise tools against a real ClickHouse.
- `test_chdb_tool.py`, `test_optional_chdb.py` exercise chDB paths.
- `test_config_interface.py`, `test_auth_config.py` are pure config tests using `monkeypatch`.
- `test_middleware.py`, `test_context_config_override.py` exercise the middleware hook and the per-request config override path. Heavy on mocks.

## Fixture and convention rules

- Reuse existing fixtures. The async `Client` fixtures in `test_mcp_server.py` are the canonical way to drive the MCP surface end to end.
- Call `load_dotenv()` as existing tests do so local `.env` settings are picked up.
- For tool-surface changes, exercise the behavior through the MCP client (`fastmcp.Client`), not only the underlying function.
- Do not rely on reimporting modules to re-read env vars. Config accessors (`get_config`, `get_chdb_config`, `get_mcp_config`) are cached singletons. Use `monkeypatch.setenv` plus patches that target the accessors, or reset via the existing fixtures.
- Prefer adding to an existing test file when the feature fits the file's theme.

## Test data conventions

- Avoid `42` as the generic representative integer.
- Avoid `alice` and `bob` as placeholders in new tests. Existing tests use them and do not need churn.
- Prefer `13`, `79`, `user_1`, `user_2`, or other neutral domain-appropriate values.

## Ad hoc validation expectations

For changes that touch query execution, destructive-op gating, read-only enforcement, auth, the `/health` endpoint, middleware loading, or pagination, do not rely only on static reasoning.

At minimum:

- Run targeted pytest coverage for the affected files.
- For transport or auth changes, run the server locally under HTTP and hit `/health` with and without an `Authorization: Bearer` header.
- For query behavior changes, validate against a real local ClickHouse from `test-services/docker-compose.yaml`.
- For chDB changes, validate with the `chdb` extra installed and `CHDB_ENABLED=true`.

## Backend-enablement matrix

Many bugs hide in the corners of the four-cell matrix. When a change touches tool registration, `/health`, or config, check all combinations:

| `CLICKHOUSE_ENABLED` | `CHDB_ENABLED` | Expected |
| --- | --- | --- |
| true (default) | false (default) | ClickHouse tools registered, chDB skipped |
| true | true | Both registered, chDB requires the `chdb` extra |
| false | true | chDB-only mode; ClickHouse config not required |
| false | false | `/health` returns 503; intentional misconfiguration |
