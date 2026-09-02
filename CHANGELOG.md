# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### Changed
- **Breaking:** The server now requires FastMCP 4, pinned to an exact release (`fastmcp==4.0.1`) because FastMCP permits breaking changes in minor versions. It builds on the MCP Python SDK v2 and speaks the sessionless `2026-07-28` protocol while still serving handshake-era clients. FastMCP 2.x and 3.x are no longer supported.
- **Breaking:** `FASTMCP_SERVER_AUTH` and `FASTMCP_SERVER_AUTH_*` are no longer supported because FastMCP 3+ removed environment-based auth provider loading. HTTP deployments that used them must set `CLICKHOUSE_MCP_AUTH_MODULE` to an importable module defining `create_auth_provider()`, which constructs the FastMCP provider with explicit keyword arguments (see `example_auth.py`). A set `FASTMCP_SERVER_AUTH` now fails startup with a migration message instead of starting unauthenticated. Exactly one of `CLICKHOUSE_MCP_AUTH_TOKEN`, `CLICKHOUSE_MCP_AUTH_MODULE`, or `CLICKHOUSE_MCP_AUTH_DISABLED=true` is still required for the HTTP transport.
- **Breaking for custom middleware:** FastMCP context state is asynchronous. Middleware that sets `clickhouse_client_config_overrides` must now `await ctx.set_state(..., serializable=False)`. FastMCP 4's default `set_state` writes to a session-scoped store keyed by `mcp-session-id` with a 24 hour TTL, so without `serializable=False` overrides set on one call would apply to every later call in the same streamable HTTP session, and non-serializable values such as `pool_mgr` would fail the tool call. As defense in depth the server removes any session-scoped copy of the key when it reads it and keeps a request-scoped copy for the rest of the request, so a value written with the default `set_state` applies to one request only; concurrent requests in one session can still observe it.
- `list_databases` and `list_tables` are now registered as async tools that run their ClickHouse work on the query worker pool and honor `CLICKHOUSE_MCP_QUERY_TIMEOUT`, so metadata calls no longer block the event loop. These tools previously had no MCP-side timeout, so a very large `list_tables` call with detailed columns that used to succeed may now need a higher `CLICKHOUSE_MCP_QUERY_TIMEOUT` or `include_detailed_columns=false`. Tool names, arguments, and JSON responses are unchanged. The exported Python helpers keep their synchronous signatures, and `create_clickhouse_client()` called without arguments now always uses the base configuration; pass overrides explicitly to apply them.
- The deprecated `sse_app()` and `streamable_http_app()` methods on the exported `mcp` server are removed, following their removal upstream. Use `http_app()`.
- Tool metadata changed with the FastMCP 4 docstring parser: `Args:` entries are now exposed as per-parameter `description` fields in `input_schema` instead of the tool description, every input schema declares `additionalProperties: false` so unknown arguments are rejected, and the `list_tables` description now states the response shape (`tables`, `next_page_token`, `total_tables`) explicitly because the parser drops docstring `Returns:` blocks.
- `CLICKHOUSE_MCP_AUTH_MODULE` factory failures now fail startup with a `ValueError` naming the module and `create_auth_provider()`, with the original exception chained.
- OAuth proxy deployments whose `issuer_url` differs from `base_url` should expect a one-time client re-authorization after moving to `CLICKHOUSE_MCP_AUTH_MODULE`: FastMCP 4 changed how the proxy derives its OAuth metadata URLs, so previously issued tokens and cached metadata are not reused (see the FastMCP 3 to 4 upgrade guide).
- The session-scoped context-state fix above matters for handshake-era sessions only: streamable HTTP clients that negotiate a session (`mcp-session-id`). Clients speaking protocol `2026-07-28` are sessionless and could never observe the leak.
- **Breaking for clients that read `structuredContent`:** every tool now declares `output_schema=None`, so tools no longer advertise a `{"result": string}` output schema and results no longer carry a `structuredContent` object whose `result` field repeats the JSON text. Release 0.5.0 (FastMCP 2.14) did emit both. The text content, a JSON-encoded string, is unchanged and is the only result representation; clients that unwrapped `structuredContent.result` and parsed the string must parse the text content instead.
- Tools now expose MCP tool annotations. `list_databases` and `list_tables` are advertised as read-only, non-destructive, idempotent, and open-world, and so is `run_chdb_select_query` with the default in-memory `CHDB_DATA_PATH`; with a filesystem data path the chDB tool is advertised as writable and destructive because it runs any SQL and the results persist. `run_query` follows the write gate: read-only and non-destructive by default; `read_only_hint=false` and `destructive_hint=true` with `CLICKHOUSE_ALLOW_WRITE_ACCESS=true`. `CLICKHOUSE_ALLOW_DROP` does not lower `destructive_hint`, because the drop guard is a best-effort keyword check that statements such as `ALTER TABLE ... MODIFY COLUMN` or `OPTIMIZE ... DEDUPLICATE` pass. `run_query` is never advertised as idempotent.
- `serverInfo.version` in the MCP `initialize` response now reports the installed `mcp-clickhouse` package version instead of the FastMCP library version, and `serverInfo.website_url` points at the project repository. When the package metadata is not installed (a bare source checkout), FastMCP falls back to its own version as before.
- FastMCP 4 derives a human-readable `title` for each tool from its name: `List Databases`, `List Tables`, `Run Query`, and `Run Chdb Select Query`. Tool names are unchanged.
- **Breaking:** `run_chdb_select_query` now reports query failures, timeouts, and unexpected errors as MCP tool errors (`isError: true`) with the message as text, the same way `run_query` does. It previously returned a successful result whose payload was `{"status": "error", "message": "..."}`. Clients that inspected the `status` field must check `isError` instead. The exported `run_chdb_select_query` Python helper raises `fastmcp.exceptions.ToolError` in the same cases.
- The `run_query` description now states which mode this server instance runs in: read-only (with a note that a `SETTINGS` clause is not permitted under `readonly=1`), writes without destructive statements, or writes including destructive statements. It also names the response shape. The chDB tool description states whether its data path is in-memory or persistent. Both descriptions still name the operator variable that changes the mode.
- The `query` parameter of `run_query` and `run_chdb_select_query` now carries a description in the input schema; it was the only undocumented parameter.

### Deprecated
- `mcp_clickhouse.table_pagination_cache`, `mcp_clickhouse.fetch_table_names_from_system`, `mcp_clickhouse.get_paginated_table_data`, and `mcp_clickhouse.create_page_token` are internal pagination helpers that were re-exported from the package namespace. Accessing them through `mcp_clickhouse` now emits a `DeprecationWarning`, and they leave `__all__` and the package namespace in the next minor release. The supported Python API is `list_databases`, `list_tables`, `run_query`, `run_chdb_select_query`, and `create_clickhouse_client`. Because the names remain in `__all__`, `from mcp_clickhouse import *` also emits the warning, which projects that run with `-W error::DeprecationWarning` will see as a failure; import the supported names explicitly.

### Removed
- **Breaking:** The SSE transport is removed. `CLICKHOUSE_MCP_SERVER_TRANSPORT=sse`, `http_app(transport="sse")`, and the `/sse` and `/messages/` endpoints are gone; the server now fails startup with a migration message if `sse` is configured. FastMCP 4 marks SSE as legacy-only, SSE sessions are permanently handshake-era so the session-scoped context-state fix above could never fully close the leak there, and this is already a breaking release. Migration: set `CLICKHOUSE_MCP_SERVER_TRANSPORT=http` and connect with a streamable HTTP client; the MCP endpoint stays at `/mcp`.

### Fixed
- `fastmcp.json` now declares every runtime dependency (`cachetools`, `simplejson`, `uvicorn`, `starlette`, `mcp`, `pydantic`) instead of three of them. `starlette`, `mcp`, and `pydantic` are also declared as direct dependencies in `pyproject.toml` because the server imports them directly. It also sets `environment.project` and `deployment.cwd` to the repository root, so `fastmcp run /path/to/fastmcp.json` now works from any directory against a source checkout (give the manifest as an absolute path: FastMCP changes directory before it resolves the path); previously the relative source path and the absolute `mcp_clickhouse.` imports made it fail outside the repository root.

## 0.5.0 - 2026-09-01

### Added
- Client connection reuse across tool calls via a config-keyed cache, eliminating per-call connection overhead. ([#152](https://github.com/ClickHouse/mcp-clickhouse/pull/152))
- Best-effort server-side query cancellation: timed-out queries now attempt `KILL QUERY` on the ClickHouse server so workers and server resources can be released. ([#152](https://github.com/ClickHouse/mcp-clickhouse/pull/152))
- `CLICKHOUSE_MCP_MAX_WORKERS` environment variable to configure the query worker thread pool size (default: `10`). ([#152](https://github.com/ClickHouse/mcp-clickhouse/pull/152))
- DNS rebinding protection for every HTTP and SSE launch path, including `fastmcp run` and `fastmcp.json`. `Host` and `Origin` headers are validated via the new `CLICKHOUSE_MCP_ALLOWED_HOSTS` and `CLICKHOUSE_MCP_ALLOWED_ORIGINS` variables: a present `Origin` that is not allow-listed is rejected with `403`, and an unknown `Host` with `421`. Authentication is now enforced whenever `fastmcp run` selects HTTP or SSE, independently of `CLICKHOUSE_MCP_SERVER_TRANSPORT`, closing a launch path that previously served unauthenticated. ([#218](https://github.com/ClickHouse/mcp-clickhouse/issues/218))
- With `CLICKHOUSE_ALLOW_WRITE_ACCESS=true` and `CLICKHOUSE_ALLOW_DROP` unset, the server now runs `SHOW GRANTS` once at first connection and logs a warning if the ClickHouse user holds `ALL`, `DROP`, `TRUNCATE`, `DELETE`, `UPDATE`, or `ALTER` (beyond `ALTER ADD`) privileges, since the destructive-operation gate is not server-enforced. The check is fail-open and never blocks startup or queries. Grants held via roles are not expanded, so the advisory only flags direct grants.

### Changed
- The minimum FastMCP version is now 2.12.3, matching the HTTP transport API used by the server, and `uvicorn>=0.31.0` is now a direct dependency (previously transitive) for trusted proxy header processing.
- `CLICKHOUSE_SEND_RECEIVE_TIMEOUT` is now auto-capped to `CLICKHOUSE_MCP_QUERY_TIMEOUT + 5` unless explicitly set, so HTTP reads unblock shortly after an MCP timeout fires. ([#152](https://github.com/ClickHouse/mcp-clickhouse/pull/152))
- **Breaking:** HTTP/SSE transports now validate `Host` and `Origin` by default, so existing deployments can change behavior on upgrade:
  - A wildcard bind (`CLICKHOUSE_MCP_BIND_HOST=0.0.0.0` or `::`) now refuses to start unless `CLICKHOUSE_MCP_ALLOWED_HOSTS` is set, because the public host cannot be inferred. Migration: set `CLICKHOUSE_MCP_ALLOWED_HOSTS` to the `host:port` values clients and reverse proxies use.
  - A request whose `Host` is not the bind address or a loopback default is rejected with `421`, and any request carrying an `Origin` not listed in `CLICKHOUSE_MCP_ALLOWED_ORIGINS` is rejected with `403`. Migration: set `CLICKHOUSE_MCP_ALLOWED_ORIGINS` for browser-based clients; non-browser clients that send no `Origin` are unaffected.
  - The `/health` endpoint remains unauthenticated and is exempt from Host and Origin validation for GET and HEAD requests, so orchestrator liveness/readiness probes can use runtime-assigned IP Hosts without extra configuration. It is reserved and cannot be used as the MCP transport path.
- The destructive-operation gate (`CLICKHOUSE_ALLOW_DROP`) now also blocks `DELETE`, `UPDATE` (including the `ALTER TABLE ... DELETE` / `UPDATE` mutations), `REPLACE TABLE` / `REPLACE PARTITION` / `CREATE OR REPLACE`, `ALTER TABLE ... CLEAR COLUMN` / `CLEAR INDEX` / `CLEAR PROJECTION`, and `DETACH ... PERMANENTLY`. These previously ran with write access alone and now require `CLICKHOUSE_ALLOW_DROP=true` as well. Plain `DETACH` stays allowed because it is reversible with `ATTACH`.
- Connection failures now log actionable hints for common misconfigurations (native TCP port used instead of the HTTP interface, TLS/`CLICKHOUSE_SECURE` mismatches), and a warning is logged when `CLICKHOUSE_PORT` is set to a native protocol port (9000/9440). ([#102](https://github.com/ClickHouse/mcp-clickhouse/issues/102))

### Fixed
- Integers outside JavaScript's safe range are now returned as decimal strings in ClickHouse and chDB tool results, preventing silent precision loss in JavaScript MCP clients. Adds a `simplejson` dependency. ([#111](https://github.com/ClickHouse/mcp-clickhouse/issues/111))
- Reverse proxies that cannot preserve the public `Host` can now configure exact proxy IP addresses or CIDR networks with `CLICKHOUSE_MCP_TRUSTED_PROXIES`. `X-Forwarded-Host` is accepted only from an immediate trusted peer and must be a single unambiguous value. The built-in HTTP/SSE runner validates Host before applying Uvicorn proxy-header processing. On dual-stack binds, IPv4 proxies observed as IPv4-mapped IPv6 peers match IPv4 entries, and IPv4-mapped CIDR entries are normalized to IPv4. Preserving `Host` at the proxy remains the preferred configuration.
- `/health` now runs ClickHouse probes outside the event loop and shares one probe across concurrent requests. A stalled probe returns `503` after two seconds instead of blocking the HTTP server.
- The exported `create_clickhouse_client` helper again preserves clickhouse-connect session ID behavior. Cache-owned MCP clients still disable autogenerated session IDs.
- `list_databases` and `list_tables` now evict stale cached clients and retry once after connection errors. `run_query` is not retried because a write may already have succeeded.
- `list_tables` now rejects non-positive `page_size` values instead of returning malformed or empty pages.
- Per-request ClickHouse client configuration overrides now reach `run_query` worker threads. Invalid override state and role aliases fail closed, nested settings preserve the configured role, and opaque client objects remain supported.
- Destructive-operation protection no longer misses `TRUNCATE` statements that omit the `TABLE` keyword (`TRUNCATE db.name` is valid ClickHouse syntax), `TRUNCATE DATABASE`, `TRUNCATE ALL TABLES FROM`, `ALTER TABLE ... DROP PARTITION` / `DROP PART` / `DROP COLUMN`, or `DROP` of object types outside `TABLE`/`DATABASE`/`VIEW`/`DICTIONARY`. With `CLICKHOUSE_ALLOW_WRITE_ACCESS=true` and `CLICKHOUSE_ALLOW_DROP` unset, these statements previously ran and deleted data.
- Destructive-operation protection no longer rejects safe statements that merely contain `drop` or `truncate` inside a string literal, a quoted identifier, or a SQL comment, such as `INSERT INTO logs VALUES ('drop the table')`. Comments can no longer hide a destructive statement from the check either.

## 0.4.1 - 2026-07-17

### Changed
- Added FastMCP server-level instructions that point agents to official ClickHouse Agent Skills (replacing tool-based advisory guidance).

## 0.4.0 - 2026-06-03

### Added
- Support for FastMCP OAuth/OIDC auth providers on HTTP/SSE transports via the `FASTMCP_SERVER_AUTH` environment variable (e.g. Azure Entra, Google, GitHub, WorkOS). Static token, FastMCP OAuth, and disabled mode are now mutually exclusive; configure exactly one. ([#171](https://github.com/ClickHouse/mcp-clickhouse/issues/171))
- Official multi-arch Docker images published to GitHub Container Registry on each release: `ghcr.io/clickhouse/mcp-clickhouse:vX.Y.Z`, `:X.Y`, and `:latest`.

### Changed
- `/health` endpoint is now unauthenticated across all auth modes (previously gated only under static-token mode, which was asymmetric and incompatible with redirect-based OAuth providers). Response bodies trimmed to `OK` / generic error strings to avoid leaking ClickHouse version information or connection exception details; underlying errors are logged server-side.

### Fixed
- Tool responses now return JSON-encoded strings, avoiding MCP protocol validation errors on successful queries. ([#154](https://github.com/ClickHouse/mcp-clickhouse/pull/154))
- Long-running queries no longer block other tool calls. The MCP-facing `run_query` and `run_chdb_select_query` tools now await their thread-pool futures asynchronously, so concurrent tool calls are served while a slow query is in flight. ([#128](https://github.com/ClickHouse/mcp-clickhouse/issues/128))

## 0.3.0 - 2026-04-14

### Added
- SNI override support via `CLICKHOUSE_SNI` environment variable for connections behind proxies or load balancers. ([#127](https://github.com/ClickHouse/mcp-clickhouse/pull/127))
- Lazy-load chdb to avoid ~80-100 MB memory overhead when the feature is disabled. ([#144](https://github.com/ClickHouse/mcp-clickhouse/pull/144))
- Made chdb an optional dependency for Windows compatibility. ([#145](https://github.com/ClickHouse/mcp-clickhouse/pull/145))
- Optional write access mode via `CLICKHOUSE_WRITE_ACCESS` environment variable, with built-in DROP and TRUNCATE protection. ([#93](https://github.com/ClickHouse/mcp-clickhouse/pull/93))
- Client config override support through MCP Context session states, enabling dynamic connection switching at runtime. ([#115](https://github.com/ClickHouse/mcp-clickhouse/pull/115))
- Custom middleware injection via `CLICKHOUSE_MCP_MIDDLEWARE` environment variable for hooking into the MCP server lifecycle. Includes an example middleware module. ([#114](https://github.com/ClickHouse/mcp-clickhouse/pull/114))

## 0.2.0 - 2026-01-28

### Added
- Basic authentication support for HTTP/SSE transport. ([#113](https://github.com/ClickHouse/mcp-clickhouse/pull/113))

## 0.1.13 - 2025-12-16

### Added
- `CLICKHOUSE_ROLE` support for setting a ClickHouse role on connections. ([#103](https://github.com/ClickHouse/mcp-clickhouse/pull/103))
- Paginated `list_tables` output. ([#92](https://github.com/ClickHouse/mcp-clickhouse/pull/92))

### Changed
- Switched to OS truststore libraries. ([#91](https://github.com/ClickHouse/mcp-clickhouse/pull/91))
- Made query timeout duration configurable. ([#89](https://github.com/ClickHouse/mcp-clickhouse/pull/89))
- Explicitly set interface based on `secure` value. ([#87](https://github.com/ClickHouse/mcp-clickhouse/pull/87))
- Switched Docker image to Alpine for smaller footprint. ([#86](https://github.com/ClickHouse/mcp-clickhouse/pull/86))

## 0.1.12 - 2025-09-15

### Changed
- Refactored chDB prompt to avoid context-too-large errors. ([#75](https://github.com/ClickHouse/mcp-clickhouse/pull/75))
- Upgraded dependencies. ([#66](https://github.com/ClickHouse/mcp-clickhouse/pull/66))

### Added
- Instructions for running without `uv`. ([#65](https://github.com/ClickHouse/mcp-clickhouse/pull/65))
- Configurable bind host and port via environment variables. ([#64](https://github.com/ClickHouse/mcp-clickhouse/pull/64))
- chDB support for local ClickHouse queries. ([#51](https://github.com/ClickHouse/mcp-clickhouse/pull/51))

## 0.1.9 - 2025-06-24

### Changed
- Migrated to fastmcp for more active upstream maintenance. ([#59](https://github.com/ClickHouse/mcp-clickhouse/pull/59))

## 0.1.8 - 2025-06-16

### Added
- Token-efficient result encoding to reduce context usage. ([#55](https://github.com/ClickHouse/mcp-clickhouse/pull/55))
- Dockerfile for containerized deployment. ([#54](https://github.com/ClickHouse/mcp-clickhouse/pull/54))
- `CLICKHOUSE_PROXY_PATH` environment variable for proxy path support. ([#52](https://github.com/ClickHouse/mcp-clickhouse/pull/52))

## 0.1.5 - 2025-03-21

### Added
- Tool descriptions for AWS Bedrock compatibility. ([#23](https://github.com/ClickHouse/mcp-clickhouse/pull/23))
- Support for parameterized views in `list_tables` with optimized row counts via system schema.
- `total_rows` and `column_count` fields in `list_tables` output. ([#32](https://github.com/ClickHouse/mcp-clickhouse/pull/32))

### Fixed
- Respect server `readonly` settings and improve query handling. ([#35](https://github.com/ClickHouse/mcp-clickhouse/pull/35))
- Ensure `.env` loaded before config init during `mcp dev` startup. ([#30](https://github.com/ClickHouse/mcp-clickhouse/pull/30))
- Prevent `BrokenResourceError` by returning structured responses for query errors. ([#26](https://github.com/ClickHouse/mcp-clickhouse/pull/26))

## 0.1.3 - 2025-02-20

### Added
- `client_name` identification header (`mcp_clickhouse`). ([#21](https://github.com/ClickHouse/mcp-clickhouse/pull/21))
- Query timeout and thread pool for SELECT queries. ([#20](https://github.com/ClickHouse/mcp-clickhouse/pull/20))
- Gather comments from ClickHouse tables for richer metadata. ([#13](https://github.com/ClickHouse/mcp-clickhouse/pull/13))
- PyPI publish GitHub Action. ([#19](https://github.com/ClickHouse/mcp-clickhouse/pull/19))

### Fixed
- Escape strings and identifiers in generated queries. ([#14](https://github.com/ClickHouse/mcp-clickhouse/pull/14))

### Changed
- Bundle system certificates as part of the MCP server. ([#15](https://github.com/ClickHouse/mcp-clickhouse/pull/15))
- Upgraded to official MCP SDK's FastMCP. ([#17](https://github.com/ClickHouse/mcp-clickhouse/pull/17))

## 0.1.1 - 2025-02-20

### Added
- Comprehensive environment configuration handling. ([#11](https://github.com/ClickHouse/mcp-clickhouse/pull/11))
- PyPI integration. ([#6](https://github.com/ClickHouse/mcp-clickhouse/pull/6))

## 0.1.0 - 2024-12-24

### Added
- Initial release of `mcp-clickhouse`.
- MCP server with `run_select_query`, `list_databases`, `list_tables` tools.
- ClickHouse connection via `clickhouse-connect`.
- CI test suite.
- Apache v2 license.
