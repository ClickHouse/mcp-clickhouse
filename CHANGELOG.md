# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### Added
- Client connection reuse across tool calls via a config-keyed cache, eliminating per-call connection overhead. ([#152](https://github.com/ClickHouse/mcp-clickhouse/pull/152))
- Server-side query cancellation: timed-out queries now issue `KILL QUERY` on the ClickHouse server instead of leaving zombie workers consuming threads and server resources. ([#152](https://github.com/ClickHouse/mcp-clickhouse/pull/152))
- `CLICKHOUSE_MCP_MAX_WORKERS` environment variable to configure the query worker thread pool size (default: `10`). ([#152](https://github.com/ClickHouse/mcp-clickhouse/pull/152))
- Support for FastMCP OAuth/OIDC auth providers on HTTP/SSE transports via the `FASTMCP_SERVER_AUTH` environment variable (e.g. Azure Entra, Google, GitHub, WorkOS). Static token, FastMCP OAuth, and disabled mode are now mutually exclusive; configure exactly one. ([#171](https://github.com/ClickHouse/mcp-clickhouse/issues/171))

### Changed
- `CLICKHOUSE_SEND_RECEIVE_TIMEOUT` is now auto-capped to `CLICKHOUSE_MCP_QUERY_TIMEOUT + 5` unless explicitly set, so HTTP reads unblock shortly after an MCP timeout fires. ([#152](https://github.com/ClickHouse/mcp-clickhouse/pull/152))
- `/health` endpoint is now unauthenticated across all auth modes (previously gated only under static-token mode, which was asymmetric and incompatible with redirect-based OAuth providers). Response bodies trimmed to `OK` / generic error strings to avoid leaking ClickHouse version information or connection exception details; underlying errors are logged server-side.

### Fixed
- Session config overrides from PR #115 are now resolved on the request thread where the FastMCP context is available, so overrides are correctly applied to queries dispatched to the worker pool. ([#152](https://github.com/ClickHouse/mcp-clickhouse/pull/152))
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
