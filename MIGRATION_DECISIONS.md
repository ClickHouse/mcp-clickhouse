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

Details settled during implementation:

- Only the async MCP-facing wrappers read request context state. The exported
  sync helpers and `create_clickhouse_client()` no longer implicitly consult
  `get_context()`; callers pass overrides explicitly. This is the only way to
  avoid awaiting inside sync code and it makes the trust boundary explicit.
- The metadata tools wait on the shared `CLICKHOUSE_MCP_QUERY_TIMEOUT` and raise
  a timeout `ToolError` like `run_query`, but do not register for `KILL QUERY`.
  The send/receive timeout cap releases the worker thread.
- The `run_http_async` guard for upstream versions lacking `uvicorn_config`, and
  the tests that faked older FastMCP signatures, are removed because the pin
  guarantees the FastMCP 4 signatures.

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

## D9. Context-state overrides require `serializable=False`; the server consumes the key

FastMCP 4 `Context.set_state` defaults to `serializable=True`, which writes to a
session-scoped store keyed by `mcp-session-id` with a 24 hour TTL. That store is
shared by every request in a streamable-HTTP session, so the request-scoped
contract in AGENTS.md only holds when middleware passes `serializable=False`,
which stores the value in FastMCP's per-request dict. This is now the documented
and required pattern (README, CHANGELOG, test middleware).

Defense in depth: `_get_client_config_overrides` deletes the key after a
successful snapshot, so a value written with the default `set_state` is consumed
by exactly one request instead of every later call in the session. Details:

- The delete runs after `_snapshot_client_config_overrides` succeeds, so an
  invalid value still fails the tool call with a `ToolError` and is not silently
  discarded.
- Concurrent requests within one session can still observe each other's
  session-scoped value before the delete runs. That race is inherent to the
  session store and is why `serializable=False` is required rather than advised.
- Opaque values such as `pool_mgr` fail inside `set_state` itself when the
  default is used; only `serializable=False` fixes that, and the boundary test
  covers it.
- The regression test drives two sequential tool calls over real streamable HTTP
  through Starlette's `TestClient`, echoing `mcp-session-id`, because
  `fastmcp.Client` never resends the header and so cannot observe the leak.

## D10. `list_tables` is registered with an explicit `description=`

FastMCP 4 parses docstrings with griffe and drops the `Returns:` block from the
tool description. The `list_tables` response shape (`tables`, `next_page_token`,
`total_tables`) is part of the public contract, so it is stated in an explicit
`description=` as `run_query` already does. An explicit description does not
suppress the per-parameter descriptions parsed from the docstring `Args:`
entries (verified against fastmcp 4.0.0). `list_databases` keeps its docstring
description because it had no `Returns:` block. The CHANGELOG records the other
parser-driven changes: `Args:` entries now appear as per-parameter `description`
fields and every input schema declares `additionalProperties: false`.

## D11. Metadata tools keep the shared query timeout without `KILL QUERY`

`list_databases` and `list_tables` previously had no MCP-side timeout. They now
fail with a `ToolError` after `CLICKHOUSE_MCP_QUERY_TIMEOUT`, which is documented
in the CHANGELOG with the mitigation (raise the timeout or pass
`include_detailed_columns=false`). Registering metadata queries for server-side
`KILL QUERY` would mean threading a `query_id` through several `system.tables`
and `system.columns` queries; not worth it for metadata reads. The docstring now
states the real worker-release behavior: the `send_receive_timeout` cap applies
only when `CLICKHOUSE_SEND_RECEIVE_TIMEOUT` is unset and not overridden by
middleware, otherwise a timed-out call can hold a worker until the HTTP read
finishes.

## D12. `http_app` auth swap is serialized; auth factory errors are wrapped

- A process-wide `threading.Lock` guards the temporary `self.auth` swap in
  `ClickHouseFastMCP.http_app`. One lock rather than per-instance because app
  construction is startup-only and this is the simplest correct shape. The test
  asserts the lock is held during the upstream call; a timing-based interleaving
  test would be flaky.
- `load_auth_provider` wraps any exception from `create_auth_provider()` in a
  `ValueError` naming `CLICKHOUSE_MCP_AUTH_MODULE`, the module, and the factory,
  with the original chained. `ValueError` rather than a new type so all
  module-shape problems surface as one exception type to `_resolve_auth`. The
  factory's own message is included verbatim, and the docstring tells operators
  not to embed secrets in exceptions.

## Review findings and resolutions

An adversarial review of the code diff ran after the first three commits.
Repro scripts live in `/Users/al/.claude/jobs/f89fd35a/tmp/` (`leak_raw.py`,
`opaque.py`) and are not committed. Note the FastMCP 4 lockfile ships `httpx2`,
not `httpx`, so ad hoc scripts need `import httpx2 as httpx`.

### R1 (high, confirmed). Overrides leak across requests within one HTTP session

Resolved by D9. `leak_raw.py` before: call 1 `["db_98"]`, call 2 `["db_98"]`.
After: call 1 `["db_98"]`, call 2 `["db_30"]`. The new HTTP session test fails on
the unpatched capture code and passes with the fix.

### R2 (high, confirmed). Opaque override values now fail the tool call

Resolved by D9. With `serializable=False` a `pool_mgr` object reaches
`clickhouse_connect.get_client` by identity (boundary test added). With the
default `set_state` the failure happens inside FastMCP before the server runs,
which is why the pattern is required.

### R3 (medium, confirmed). `list_tables` validation errors name the wrapper

Resolved. `__name__` and `__qualname__` on both async wrappers are set to the
tool names; a boundary test asserts the `page_size=0` error names `list_tables`
and not `list_tables_async`.

### R4 (medium, confirmed). Tool descriptions and schemas changed under FastMCP 4

Resolved by D10 and the CHANGELOG entry.

### R5 (medium). Metadata tool timeout is new, untested, and under-documented

Resolved by D11. `_run_metadata_tool` now has unit tests for success, timeout
(`ToolError` text and `logger.warning`), `ToolError` and generic exception
propagation, and caller cancellation. `mcp_env.py` describes the variable as the
timeout for `run_query`, `list_databases`, and `list_tables`, matching the README.

### R6 (low). Session store growth under the documented middleware pattern

Resolved by D9. `serializable=False` never touches the session store, and the
delete-after-capture removes any entry written by legacy middleware.

### R7 (low, pre-existing). `self.auth` swap in `http_app` is not thread-safe

Resolved by D12.

### R8 (low). `mcp_auth_hook` docstring overstates guarantees

Resolved by D12.

### Test-quality notes from the review

- The three `run_http_async` tests keep their `**kwargs` fakes because they
  exercise the kwargs-merging logic. A new test binds the exact keyword sets the
  CLI and the built-in runner forward against the real
  `inspect.signature(FastMCP.run_http_async)`, so a signature change upstream
  fails loudly.
- `AsyncMock` usage is genuine, and the list-tool boundary tests assert end to
  end. Removed FastMCP 2.12/2.13 tests left no coverage gap.
- Categories with nothing found: `get_context()` from worker threads, `ToolError`
  typing through the executor, unauthenticated HTTP/SSE paths, `/health`,
  exported symbols, JSON response bodies.
