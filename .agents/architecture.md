# AI Architecture And Repo Context

This document provides repo-specific context for AI agents working in `mcp-clickhouse`.

`AGENTS.md` is the operational source of truth. This file is required reading before substantial code changes, but it does not override `AGENTS.md`.

## Repository Overview

`mcp-clickhouse` is an MCP (Model Context Protocol) server that exposes ClickHouse and chDB to MCP clients such as Claude Desktop. It is built on `FastMCP` and uses `clickhouse-connect` as the ClickHouse HTTP driver. The package is published to PyPI as `mcp-clickhouse` and is distributed as a container image.

Top-level areas that matter most:

```text
mcp_clickhouse/
  mcp_server.py            FastMCP server, tool registration, query execution, pagination, health check
  mcp_env.py               Environment-driven config (ClickHouseConfig, ChDBConfig, MCPServerConfig) with singletons
  main.py                  Entry point. Resolves transport and starts the server
  mcp_middleware_hook.py   Loads user middleware from MCP_MIDDLEWARE_MODULE
  chdb_prompt.py           Prompt text returned by the chdb_initial_prompt prompt
tests/                     pytest suite. Most tests expect a live ClickHouse on localhost
test-services/             docker-compose for a local ClickHouse for test and development
example_middleware.py      Reference middleware module for MCP_MIDDLEWARE_MODULE
fastmcp.json               FastMCP project manifest pointing at mcp_clickhouse/mcp_server.py
Dockerfile                 Container image build
```

## Core Invariants

### The tool surface is the public API

This server is an MCP server. The protocol surface is what users and AI clients depend on. Treat the following as public:

- Tool names: `run_query`, `list_databases`, `list_tables`, `run_chdb_select_query`.
- Tool argument names, types, defaults, and docstrings (docstrings become tool descriptions visible to clients).
- Tool return shapes, including:
  - `list_tables` returns `{"tables": [...], "next_page_token": str | None, "total_tables": int}`.
  - `run_query` returns `{"columns": [...], "rows": [...]}` on success or `{"status": "error", "message": str}` on handled failure.
  - `run_chdb_select_query` returns a list of row dicts on success or `{"status": "error", "message": str}` on failure.
  - `list_databases` returns a JSON-encoded string (intentional, existing contract).
- Prompt names such as `chdb_initial_prompt`.
- The `/health` custom route and its status code contract (`200` healthy, `503` unhealthy, `401` unauthorized when auth is enabled).

Small internal refactors can still produce breaking changes at this surface.

### Environment variables are public

All documented `CLICKHOUSE_*`, `CHDB_*`, `CLICKHOUSE_MCP_*`, and `MCP_MIDDLEWARE_MODULE` variables are public. Names, defaults, and semantics are compatibility commitments. Changes here require README updates and clear intent.

When adding new configuration, thread it through `mcp_clickhouse/mcp_env.py` rather than reading `os.environ` in tool code.

### Safety defaults are intentional

Three layered safety defaults ship on purpose:

1. Queries run with `readonly=1` unless `CLICKHOUSE_ALLOW_WRITE_ACCESS=true`.
2. Even with writes enabled, destructive statements (DROP TABLE, DROP DATABASE, DROP VIEW, DROP DICTIONARY, TRUNCATE TABLE) are rejected unless `CLICKHOUSE_ALLOW_DROP=true`. The check lives in `_validate_query_for_destructive_ops` as a regex scan.
3. HTTP and SSE transports require an auth token via `CLICKHOUSE_MCP_AUTH_TOKEN` unless `CLICKHOUSE_MCP_AUTH_DISABLED=true` is set explicitly.

Do not relax these defaults casually. When touching `get_readonly_setting`, `_validate_query_for_destructive_ops`, or the auth wiring in `mcp_server.py`, keep the behavior matrix intact and update tests.

### ClickHouse and chDB are two independently optional backends

Either, both, or (intentionally) neither can be enabled:

- `CLICKHOUSE_ENABLED` (default `true`) gates `list_databases`, `list_tables`, and `run_query`.
- `CHDB_ENABLED` (default `false`) gates `run_chdb_select_query` and `chdb_initial_prompt`.
- chDB requires the `chdb` optional extra. If `chdb` is not installed, the server logs a warning and skips chDB tool registration rather than crashing. Preserve that behavior: do not make `chdb` a hard import at module load.
- The `/health` endpoint accounts for all combinations and returns `503` if both backends are effectively disabled or unreachable.

Changes to backend enablement should keep all four combinations working.

### Configuration is a singleton

`get_config()`, `get_chdb_config()`, and `get_mcp_config()` return cached instances. The cache is process-wide.

- In tests, use `monkeypatch.setenv` plus patches that target the config accessors, or reset the global via the existing fixtures. Do not rely on reimporting modules to re-read env vars.
- `ClickHouseConfig.__init__` validates required variables only when the backend is enabled. Preserve this. It is why the server can run in chDB-only mode without `CLICKHOUSE_HOST`.

### Context-based client config overrides

`create_clickhouse_client` merges per-request overrides from the FastMCP context under `CLIENT_CONFIG_OVERRIDES_KEY` on top of the base config. This is how middleware injects per-request routing, tenant overrides, and timeouts.

- Non-dict values under this key are ignored with a warning. Keep that behavior.
- When outside a request context (for example, at startup or in unit tests), `get_context()` raises `RuntimeError` and the code falls back to the base config. Preserve this fallback.

### Query execution runs through a bounded thread pool

`QUERY_EXECUTOR` is a `ThreadPoolExecutor(max_workers=10)` registered for `atexit` shutdown. Both `run_query` and `run_chdb_select_query` submit work to it and enforce `CLICKHOUSE_MCP_QUERY_TIMEOUT`.

- The timeout is applied at the Python level via `Future.result(timeout=...)`. The query is not canceled server-side. Be aware of that if you touch cancellation or timeout logic.
- Exceeding the pool size under load queues work. If you raise throughput expectations, think about whether the pool size or the model needs to change.

### Pagination state lives in a TTL cache

`table_pagination_cache` is a `cachetools.TTLCache(maxsize=100, ttl=3600)`. Tokens are UUIDs.

- Tokens are invalidated across filter changes (`database`, `like`, `not_like`, `include_detailed_columns`). The server logs a warning and restarts from the beginning. Preserve that defensive behavior.
- The cache is process-local. It is not safe to rely on pagination state surviving server restarts or working across replicas.

### Middleware is user-loadable code

`MCP_MIDDLEWARE_MODULE` lets users inject arbitrary middleware by module name. The server calls `module.setup_middleware(mcp)`.

- The loader lives in `mcp_middleware_hook.py`. It logs and reraises import errors.
- Documented FastMCP hooks (`on_call_tool`, `on_request`, `on_read_resource`, and siblings) are part of the user contract. Do not paper over FastMCP hook contract changes silently.
- `example_middleware.py` is the reference for docs and tests. Keep it runnable.

### TLS is handled through truststore by default

`mcp_clickhouse/__init__.py` calls `truststore.inject_into_ssl()` at import time unless `MCP_CLICKHOUSE_TRUSTSTORE_DISABLE=1` is set. This is why the server trusts system certificates out of the box. Do not move or remove this without understanding the implications for users on corporate networks.

## Compatibility Matrix

Keep these axes in mind when evaluating change risk:

- Python `3.10+` (the declared floor in `pyproject.toml`). CI currently exercises Python `3.13`.
- ClickHouse server: CI runs against `clickhouse/clickhouse-server:24.10`. Behavior should stay reasonable across recent ClickHouse versions.
- `fastmcp` `>=2.0.0,<3.0.0`.
- `clickhouse-connect` `>=0.8.16`.
- Transports: `stdio`, `http`, `sse`. Most users are on `stdio`. HTTP and SSE are exercised when authentication or the health endpoint matter.
- Optional extras: bare install (no chDB) must keep working. `chdb` extra is exercised in CI with `CHDB_ENABLED=true`.
- Container image: the Dockerfile is built in CI and the image must at least import cleanly and start under the default command.

## Performance And Resource Considerations

The server is not a hot path like a driver, but a few areas matter:

- `list_tables` makes one query per table for detailed column metadata. `include_detailed_columns=False` exists specifically for large schemas. Preserve that escape hatch and do not regress the batching in `get_paginated_table_data`.
- The query thread pool caps concurrency. Tool behavior under timeouts must stay predictable.
- Avoid per-row Python overhead in anything that touches large result sets. The server returns raw rows from `clickhouse-connect` without rebuilding them, which is intentional.

## Testing Layout

Tests live in `tests/`. Most expect a live ClickHouse on `localhost:8123`.

- `test_tool.py`, `test_mcp_server.py`, `test_pagination.py` exercise tools against a real server.
- `test_chdb_tool.py` and `test_optional_chdb.py` exercise chDB paths. chDB ones require `CHDB_ENABLED=true` and the extra.
- `test_config_interface.py` and `test_auth_config.py` are pure config tests with `monkeypatch`.
- `test_middleware.py` and `test_context_config_override.py` exercise the middleware hook and the per-request config override path. These use mocks heavily.

When adding a test:

- Prefer adding to an existing file when the feature fits.
- If you need a new fixture, check `test_mcp_server.py` first. Its async `Client` fixtures are the canonical way to drive the MCP surface end to end.
- Use `load_dotenv()` as existing tests do so local `.env` settings are picked up.

## Ad Hoc Validation Expectations

For changes that touch query execution, destructive-op gating, read-only enforcement, auth, the health endpoint, middleware loading, or pagination, do not rely only on static reasoning.

At minimum:

- Run targeted pytest coverage for the affected files.
- For transport or auth changes, run the server locally under HTTP and hit `/health` with and without the `Authorization: Bearer` header.
- For query behavior changes, validate against a real local ClickHouse started from `test-services/docker-compose.yaml`.
- For chDB changes, validate with the `chdb` extra installed and `CHDB_ENABLED=true`.

## How To Use This Doc

Use this file to understand what is structurally important in the repo before changing code.

Use `.agents/review.md` when the task is specifically code review, review feedback, or patch analysis.
