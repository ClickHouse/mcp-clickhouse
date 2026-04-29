# Architecture and repo context

Read this before substantial code changes. For test commands and validation, see `.agents/skills/testing/SKILL.md`. For review work, see `.agents/skills/review/SKILL.md`. For library or server claims, see `.agents/skills/upstream-verify/SKILL.md`.

## What this is

`mcp-clickhouse` is an MCP server that exposes ClickHouse and chDB to MCP clients (Claude Desktop, Cursor, etc.). It is built on `FastMCP` and uses `clickhouse-connect` as the HTTP driver. The package ships to PyPI as `mcp-clickhouse` and as a container image.

## Layout

```text
mcp_clickhouse/
  mcp_server.py            FastMCP server, tool registration, query execution, pagination, health check
  mcp_env.py               Environment-driven config (ClickHouseConfig, ChDBConfig, MCPServerConfig) with singletons
  main.py                  Entry point. Resolves transport and starts the server
  mcp_middleware_hook.py   Loads user middleware from MCP_MIDDLEWARE_MODULE
  chdb_prompt.py           Prompt text returned by the chdb_initial_prompt prompt
tests/                     pytest suite. Most tests expect a live ClickHouse on localhost
test-services/             docker-compose for local ClickHouse for test and development
example_middleware.py      Reference middleware module for MCP_MIDDLEWARE_MODULE
fastmcp.json               FastMCP project manifest pointing at mcp_clickhouse/mcp_server.py
Dockerfile                 Container image build
```

## Cross-cutting invariants

Function-level invariants (singleton config caching, context-override handling, truststore inject, thread-pool timeout semantics) live in docstrings on the relevant code. The items below are the cross-cutting ones that do not belong in any single function.

### Layered safety defaults

Three defaults ship at the safer setting on purpose:

1. Queries run with `readonly=1` unless `CLICKHOUSE_ALLOW_WRITE_ACCESS=true`.
2. Even with writes enabled, destructive statements (`DROP TABLE`, `DROP DATABASE`, `DROP VIEW`, `DROP DICTIONARY`, `TRUNCATE TABLE`) are rejected unless `CLICKHOUSE_ALLOW_DROP=true`. The check is a regex scan in `_validate_query_for_destructive_ops`.
3. HTTP and SSE transports require an auth token via `CLICKHOUSE_MCP_AUTH_TOKEN` unless `CLICKHOUSE_MCP_AUTH_DISABLED=true` is set explicitly.

When touching `get_readonly_setting`, `_validate_query_for_destructive_ops`, or the auth wiring in `mcp_server.py`, keep the behavior matrix intact and update tests.

### Two independently optional backends

ClickHouse and chDB can each be enabled or disabled. All four combinations must keep working:

- `CLICKHOUSE_ENABLED` (default `true`) gates `list_databases`, `list_tables`, `run_query`.
- `CHDB_ENABLED` (default `false`) gates `run_chdb_select_query` and `chdb_initial_prompt`.
- chDB requires the `chdb` optional extra. If the package is missing, the server warns and skips chDB tool registration. Do not make `chdb` a hard import at module load.
- `/health` accounts for all combinations. It returns `503` if both backends are effectively disabled or unreachable.

### Tool surface and env vars are public

These are the contract. Treat all changes here as breaking and update `README.md` alongside any intentional change. `AGENTS.md` lists the surface explicitly.

### Pagination state is process-local

`table_pagination_cache` is a `cachetools.TTLCache(maxsize=100, ttl=3600)`. Tokens are UUIDs. Tokens are invalidated across filter changes (`database`, `like`, `not_like`, `include_detailed_columns`); the server logs a warning and restarts from the beginning. Pagination state does not survive process restarts and is not safe across replicas.

### Middleware is user-loadable code

`MCP_MIDDLEWARE_MODULE` lets users inject arbitrary middleware by module name. The loader (`mcp_middleware_hook.py`) calls `module.setup_middleware(mcp)` and reraises import errors. Documented FastMCP hooks (`on_call_tool`, `on_request`, `on_read_resource`, etc.) are part of the user contract. `example_middleware.py` is the reference and must stay runnable.

## Performance and resource notes

The server is not a hot path, but a few areas matter:

- `list_tables` makes one query per table for detailed column metadata. `include_detailed_columns=False` is the escape hatch for large schemas. Preserve the batching in `get_paginated_table_data`.
- The query thread pool caps concurrency at 10. Tool behavior under timeouts must stay predictable. `CLICKHOUSE_MCP_QUERY_TIMEOUT` is enforced at the Python level via `Future.result(timeout=...)`; the query is not canceled server-side.
- Large result sets are returned raw from `clickhouse-connect` without rebuilding. Avoid per-row Python overhead.

## Compatibility axes

- Python `3.10+`. CI exercises Python `3.13`.
- ClickHouse server: CI uses `clickhouse/clickhouse-server:24.10`. Behavior should stay reasonable across recent versions.
- `fastmcp` `>=2.0.0,<3.0.0`.
- `clickhouse-connect` `>=0.8.16`.
- Transports: `stdio`, `http`, `sse`.
- Optional extras: bare install (no chDB) must keep working. The `chdb` extra is exercised in CI.
- Container image: built in CI; must at least import and start under the default command.

## When code and this doc disagree

The code wins. If you find a divergence while working, fix the doc in the same PR rather than letting it rot.
