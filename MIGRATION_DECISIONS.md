# FastMCP 4 Migration Decisions

Working notes for the `fastmcp-4` branch. Each entry records a call that was made
without waiting for review so the work could keep moving. Any of them can be
revisited.

## D1. Target `fastmcp>=4.0.0,<5.0.0`, drop FastMCP 2 and 3 support

FastMCP 4.0.0 shipped 2026-08-31. Every break found in this repo was introduced
in FastMCP 3.0, so supporting 2.x alongside 4 would require shims (for example
`inspect.isawaitable` around `ctx.get_state`). Two majors of compatibility code
is not worth carrying. The constraint is a range, not an exact pin, because this
is a distributed package rather than an application.

## D2. Context state access becomes async only, no compatibility shim

`Context.get_state` and `Context.set_state` are coroutines in FastMCP 3+. The
request-scoped override capture is rewritten to `await` them. Middleware authors
who set `clickhouse_client_config_overrides` must `await ctx.set_state(...)`.
This is a documented breaking change for custom middleware modules.

## D3. `list_databases` and `list_tables` are registered through async wrappers

FastMCP 4 runs sync tool functions on an AnyIO worker thread by default. A sync
function cannot `await ctx.get_state`, so the MCP-facing versions of these two
tools become async wrappers that capture overrides on the event loop and run the
existing sync helpers on `QUERY_EXECUTOR`, matching `run_query_async`. The
exported sync helpers (`mcp_clickhouse.list_databases`, `list_tables`) keep their
signatures so direct Python callers are unaffected. Tool names, arguments,
docstrings, and JSON output are unchanged.

## D4. `FASTMCP_SERVER_AUTH` env-var auto-loading is replaced by a module hook

FastMCP 3+ removed `settings.server_auth_class` and per-provider
`FASTMCP_SERVER_AUTH_*` pydantic-settings. Provider constructors now take
explicit keyword arguments. Rather than reimplement and maintain FastMCP's
abandoned env-var mapping, the server gains `CLICKHOUSE_MCP_AUTH_MODULE`: an
importable module exposing `create_auth_provider() -> AuthProvider`, mirroring the
existing `MCP_MIDDLEWARE_MODULE` hook. Exactly one of `CLICKHOUSE_MCP_AUTH_TOKEN`,
`CLICKHOUSE_MCP_AUTH_MODULE`, or `CLICKHOUSE_MCP_AUTH_DISABLED=true` is still
required for HTTP/SSE. Setting `FASTMCP_SERVER_AUTH` now fails startup with a
message pointing at the new variable, so a silent unauthenticated start is
impossible.

Details settled during implementation:

- The hook contract is `create_auth_provider()` with no arguments, called on every
  `http_app` construction (normally once at startup), not cached.
- The result must be an instance of `fastmcp.server.auth.AuthProvider`, so custom
  `TokenVerifier` subclasses and OAuth proxies are accepted alike.
- `FASTMCP_SERVER_AUTH` is rejected only when an HTTP/SSE app is built. Stdio
  never authenticates, so a stale variable does not break stdio users.
- The rejection fires even when a valid mode is also configured, so a
  half-migrated environment fails loudly instead of silently switching mechanism.
- A blank or whitespace-only `CLICKHOUSE_MCP_AUTH_MODULE` is treated as unset,
  matching how an empty `CLICKHOUSE_MCP_AUTH_TOKEN` behaves.
- `example_auth.py` shows the pattern with `JWTVerifier` built from explicit
  environment variables.

## D5. Remove the `sse_app` and `streamable_http_app` overrides

Upstream removed both methods in FastMCP 3. They existed here only to force the
deprecated builders through the secured `http_app`. With no upstream caller they
are dead code, and `sse_app` depended on the removed `_deprecated_settings`. The
overrides and their tests are deleted. `http_app` and `run_http_async` remain the
single secured entry points.

## D6. Camelcase compatibility bridge is disabled in CI

The MCP SDK v2 renamed protocol fields to snake_case. FastMCP 4 keeps a warning
bridge for camelCase reads. The one repo read (`Tool.inputSchema`) is fixed and
CI sets `FASTMCP_MCP_CAMELCASE_COMPAT=false` so new legacy reads fail loudly.

## D7. FastMCP's built-in Host/Origin protection stays off

FastMCP 4 ships `host_origin_protection`, disabled by default. The repo keeps its
own `DNSRebindingProtectionMiddleware`, which also covers trusted proxies and the
`/health` exemption. Enabling both would double-validate with different status
codes. A code comment records this.

## D8. No version bump on this branch

`pyproject.toml` version and `server.json` are left for release preparation. The
CHANGELOG gets an `Unreleased` section.
