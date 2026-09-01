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

## Open review findings (not yet addressed)

An adversarial review of the code diff ran after the three commits. Findings are
recorded here so work can resume. None have been fixed yet. Repro scripts live in
`/Users/al/.claude/jobs/f89fd35a/tmp/` (`leak_raw.py`, `opaque.py`) and are not
committed.

### R1 (high, confirmed). Overrides leak across requests within one HTTP session

FastMCP 4 `Context.set_state` defaults to `serializable=True`, which writes to a
session-scoped in-memory store keyed by `mcp-session-id` with a 24 hour TTL. The
migration adopted `await ctx.set_state(...)` without `serializable=False`, so a
middleware that sets overrides on one call leaves them active for every later
call in the same streamable-HTTP session. Confirmed with a raw HTTP client that
echoes `mcp-session-id`; `fastmcp.Client` does not resend the header, which is
why the suite did not catch it. This violates the request-scoped contract in
AGENTS.md and the README text added in this branch is wrong on this point.
Proposed fix: document and use `serializable=False` in the README example and
`example_middleware.py`, and have `_get_client_config_overrides` delete the key
after capture as defense in depth. Add a boundary test that drives two sequential
calls over real streamable HTTP with the session header.

### R2 (high, confirmed). Opaque override values now fail the tool call

Same root cause. `_snapshot_client_config_overrides` deliberately preserves
non-serializable objects such as `pool_mgr`, but `set_state(serializable=True)`
raises `TypeError` for them and the client sees an internal server error.
`serializable=False` fixes this as well.

### R3 (medium, confirmed). `list_tables` validation errors name the wrapper

Pydantic error text now reads `call[list_tables_async]` instead of
`call[list_tables]`. Set `__name__` on both async wrappers to the tool name.

### R4 (medium, confirmed). Tool descriptions and schemas changed under FastMCP 4

FastMCP 4 parses docstrings with griffe. The `Returns:` block of `list_tables`
(documenting `tables`, `next_page_token`, `total_tables`) is dropped from the
description; `Args:` entries moved into per-parameter descriptions; all input
schemas gained `additionalProperties: false`. Not caused by repo code, but a
public-contract change to acknowledge in the CHANGELOG. If the response shape
matters, move it into an explicit `description=` as `run_query` already does.

### R5 (medium). Metadata tool timeout is new, untested, and under-documented

`list_databases` and `list_tables` previously had no timeout; they are now
capped by `CLICKHOUSE_MCP_QUERY_TIMEOUT` (default 30s). A long `list_tables`
with detailed columns that used to succeed can now fail. The
`_run_metadata_tool` docstring claims the send/receive cap releases the worker,
but that cap is skipped when `CLICKHOUSE_SEND_RECEIVE_TIMEOUT` is set or
overridden, so a timed-out metadata call can pin a worker with no `KILL QUERY`.
No tests cover `_run_metadata_tool` timeout or error propagation. `mcp_env.py`
still describes the variable as a SELECT tool timeout.

### R6 (low). Session store growth under the documented middleware pattern

With `serializable=True`, every tool call writes a 24 hour entry into an
unbounded process-wide store. `serializable=False` avoids the store entirely.

### R7 (low, pre-existing). `self.auth` swap in `http_app` is not thread-safe

Two concurrent `http_app()` calls on one instance could interleave so that the
second builds an unauthenticated app. Same shape as on `main`; startup-only in
practice. A `threading.Lock` around the swap removes it.

### R8 (low). `mcp_auth_hook` docstring overstates guarantees

The factory call has no exception handling, so operator errors propagate
verbatim, and only `ImportError` is wrapped with the env var name. No secret is
echoed by repo code; this is docstring accuracy plus error ergonomics.

### Test-quality notes from the review

- Three `run_http_async` tests monkeypatch the upstream with a `**kwargs` fake,
  so nothing now validates binding against the real FastMCP 4 signature.
- `AsyncMock` usage is genuine, and the new list-tool boundary test asserts end
  to end. Removed FastMCP 2.12/2.13 tests left no coverage gap.
- Categories with nothing found: `get_context()` from worker threads, `ToolError`
  typing through the executor, unauthenticated HTTP/SSE paths, `/health`,
  exported symbols, JSON response bodies.
