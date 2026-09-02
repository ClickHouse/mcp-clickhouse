# FastMCP 4 Migration Decisions

Working notes for the `fastmcp-4` branch. Each entry records a call that was made
without waiting for review so the work could keep moving. Any of them can be
revisited.

## D1. Target FastMCP 4, drop FastMCP 2 and 3 support

FastMCP 4.0.0 shipped 2026-08-31. Every break found in this repo was introduced
in FastMCP 3.0, so supporting 2.x alongside 4 would require shims (for example
`inspect.isawaitable` around `ctx.get_state`). Two majors of compatibility code
is not worth carrying. The branch initially used the range `>=4.0.0,<5.0.0`;
superseded by D19, which pins an exact release.

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

Defense in depth: `_get_client_config_overrides` consumes the key in a fixed
order after `get_state` returns a value:

1. `delete_state` removes any session-scoped copy, so a value written with the
   default `set_state` applies to one request only, and an invalid value cannot
   poison every later request in the session for its 24 hour TTL.
2. `set_state(..., serializable=False)` restores a request-scoped copy, so a
   second read within the same MCP request (for example under FastMCP's own
   `RetryMiddleware`) sees the same overrides instead of silently running on the
   base configuration. Deleting without restoring was the first version of this
   fix and the second review caught the retry regression.
3. The snapshot validates last; an invalid value still fails this request's
   tool call with a `ToolError`.

Further details:

- Concurrent requests within one session can still observe each other's
  session-scoped value before the delete runs. That race is inherent to the
  session store and is why `serializable=False` is required rather than advised.
- The leak applies to any transport with a stable session id: streamable HTTP
  with a negotiated session and SSE. Stdio issues a fresh session id per tool
  call, so the session store is write-only there and nothing leaks.
- Opaque values such as `pool_mgr` fail inside `set_state` itself when the
  default is used; only `serializable=False` fixes that, and the boundary test
  covers it.
- The regression tests drive sequential tool calls over real streamable HTTP
  through Starlette's `TestClient`, echoing `mcp-session-id`, because
  `fastmcp.Client` does not resend the header on streamable HTTP and so cannot
  observe the leak there. `fastmcp.Client` over SSE would observe it, but that
  needs a live uvicorn server; no SSE-specific test was added.

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
and `system.columns` queries; not worth it for metadata reads.

`list_tables` issues up to `page_size + 2` sequential queries, so the
per-request `send_receive_timeout` cap alone did not bound how long a timed-out
call held a `QUERY_EXECUTOR` worker. `list_tables_async` now passes a
`time.monotonic()` deadline through `_list_tables_with_config`,
`_list_tables_impl`, and `get_paginated_table_data` (a new optional trailing
keyword on the public helper), and the worker raises between queries once the
MCP timeout has passed. A single stalled query still holds the worker until the
client's `send_receive_timeout`, which is capped near the MCP timeout only when
`CLICKHOUSE_SEND_RECEIVE_TIMEOUT` is unset and not overridden by middleware.
The redundant `CancelledError` branch in `_run_metadata_tool` was removed;
`asyncio.wrap_future` already propagates cancellation to a queued future, and
the test saturates the executor to prove it.

## D12. `http_app` auth swap is serialized; auth factory errors are wrapped

- A process-wide `threading.Lock` guards the temporary `self.auth` swap in
  `ClickHouseFastMCP.http_app`. One lock rather than per-instance because app
  construction is startup-only and this is the simplest correct shape. The test
  asserts the lock is held during the upstream call; a timing-based interleaving
  test would be flaky.
- `load_auth_provider` wraps any exception from `create_auth_provider()` in a
  `ValueError` naming `CLICKHOUSE_MCP_AUTH_MODULE`, the module, and the factory,
  with the original chained. A missing or non-callable factory and a wrong
  return type are also `ValueError`s naming the variable; an unimportable module
  stays `ImportError`, as before. The factory's own message is included
  verbatim, and the docstring tells operators not to embed secrets in
  exceptions. An `ImportError` raised inside the factory (a provider with a
  missing optional dependency) now surfaces as `ValueError`; nothing in the repo
  depended on the old type.
- The lock test proves mutual exclusion: the first construction blocks inside a
  fake upstream `http_app` while a second construction is shown to wait.

## D13. chDB tool errors stay a successful result

- `run_chdb_select_query` reports query failures, timeouts, and unexpected
  exceptions as a successful tool result whose JSON payload is
  `{"status": "error", "message": "..."}` (`isError` false), unlike `run_query`,
  which raises `ToolError`. The shape predates the migration (chDB support in
  #51) and the existing sync helper tests assert it, so under AGENTS.md it is
  part of the public tool contract. It stays unchanged and is now pinned at the
  MCP boundary in `tests/test_chdb_tool.py`. Aligning it with `run_query` would
  be a breaking change for a separate release.
- Follow-up: the README chDB section documents neither error contract. Add a
  line describing the payload shape.

## D14. Tool annotations track the write gate

- Every tool carries MCP `ToolAnnotations` (imported from `mcp.types`,
  snake_case fields). `list_databases`, `list_tables`, and
  `run_chdb_select_query` are `read_only_hint=True`, `destructive_hint=False`,
  `idempotent_hint=True`, `open_world_hint=True`.
- `run_query` annotations are computed once at registration by
  `_run_query_annotations(ClickHouseConfig())`: read-only mode ->
  `read_only_hint=True`, `destructive_hint=False`;
  `CLICKHOUSE_ALLOW_WRITE_ACCESS=true` -> `read_only_hint=False`,
  `destructive_hint=True`. `idempotent_hint` is always False and
  `open_world_hint` always True.
- The handover specified `destructive_hint=False` for write access without
  `CLICKHOUSE_ALLOW_DROP`. Rejected after review: the MCP spec reads
  `destructiveHint=false` as "performs only additive updates", but the drop
  gate is `_validate_query_for_destructive_ops`, a keyword regex the server
  itself describes as a best-effort accident guard. `ALTER TABLE ... MODIFY
  COLUMN`, `OPTIMIZE ... DEDUPLICATE`, `ALTER TABLE ... MOVE PARTITION`,
  `ALTER TABLE ... MODIFY TTL`, and `RENAME TABLE` all pass it. A client that
  auto-approves non-destructive tools would auto-approve those, so write access
  is advertised as destructive regardless of the drop gate.
- With write access on but the server enforcing `readonly=1`, writes are
  impossible yet `read_only_hint=False` is still advertised. That direction is
  conservative and accepted.
- Registration moved into `_register_clickhouse_tools(server)` so tests can
  register on a fresh `FastMCP` with a patched config and assert through
  `Client.list_tools()`.
- Registration reads the gates from a throwaway `ClickHouseConfig()` rather
  than `get_config()`. The constructor runs `_validate_required_vars()` once
  and `get_config()` caches the instance, so calling it at import would snapshot
  the validation result: a caller that imports the package and then changes
  `CLICKHOUSE_*` would later get a raw `KeyError` from a property instead of
  the friendly `ValueError` the lazy first-call path produces. With the
  throwaway instance `_CONFIG_INSTANCE` stays `None` after import and the
  pre-existing lazy validation is untouched (asserted in
  `tests/test_tool_contract.py`).
- Importing with `CLICKHOUSE_ENABLED=true` but no `CLICKHOUSE_HOST` has never
  failed (the first tool call reports it). Registration therefore catches
  `ValueError` from the constructor, logs a WARNING, and advertises the default
  read-only annotations rather than turning a lazy runtime error into an import
  failure. The warning is new and will appear in import smoke tests with an
  incomplete config.
- AGENTS.md records annotations as part of the observable contract.

## D15. `output_schema=None` on every tool

- FastMCP 4 derives `{"properties": {"result": {"type": "string"}},
  "x-fastmcp-wrap-result": true}` from the `-> str` annotation and returns the
  JSON string a second time as `structured_content` on every result. All four
  `Tool.from_function` calls pass `output_schema=None`, which suppresses both
  (verified: `list_tools()` output_schema is None, `call_tool()`
  structured_content is None). The JSON-string text contract is unchanged.
  Recorded in AGENTS.md next to the JSON-string bullet.
- FastMCP 4 also derives tool titles ("List Databases", "List Tables",
  "Run Query", "Run Chdb Select Query"). Accepted and noted in the CHANGELOG.

## D16. `serverInfo` reports the package version

- `ClickHouseFastMCP` is constructed with
  `version=importlib.metadata.version("mcp-clickhouse")` and
  `website_url="https://github.com/ClickHouse/mcp-clickhouse"` (pyproject
  `[project.urls] Home`), so `initialize` returns the project version instead
  of FastMCP's 4.0.0. `_package_version()` returns None when the distribution is
  not installed so a source checkout without `uv sync` still imports; FastMCP
  then substitutes its own version (`version or fastmcp.__version__`), so that
  case behaves exactly as before this change.

## D17. `page_size` guard kept in `_list_tables_with_config`

- The `page_size <= 0` guard is unreachable through the MCP boundary (pydantic
  `Field(gt=0)` rejects first) but reachable through the exported synchronous
  `list_tables` helper, which bypasses pydantic. It stays, with a direct test
  that it raises `ToolError` before any ClickHouse client is acquired.

## D18. Packaging declares what the server imports

- `starlette>=1.0.1`, `mcp>=2.0.0,<3.0.0`, and `pydantic>=2.0` are direct
  dependencies in `pyproject.toml` because `mcp_server.py` (and
  `http_security.py` for starlette) import them directly; they were previously
  only transitive through FastMCP. The `mcp` import (`ToolAnnotations`) is new
  in this branch. `uv add` regenerated the lockfile (metadata lines only, no
  resolution change).
- `fastmcp.json` `environment.dependencies` now lists `cachetools`,
  `simplejson`, `uvicorn`, `starlette`, `mcp`, and `pydantic` alongside the
  original three. `tests/test_packaging_metadata.py` asserts the two lists
  match, using `importlib.metadata.requires` rather than `tomllib` (3.11+);
  packages declared as optional extras (`chdb`) may appear in `fastmcp.json`
  without failing the stale-dependency direction.
- The dependency list alone does not make `fastmcp run fastmcp.json` work from
  an arbitrary directory. FastMCP's filesystem source puts only the file's own
  directory (`mcp_clickhouse/`) on `sys.path`, and `mcp_server.py` uses
  absolute `mcp_clickhouse.` imports, so the project itself must be installed.
  From the repository root `uv run` installs it editable, at which point every
  pyproject dependency is already present. The CHANGELOG therefore says
  "declares", not "fixes startup".
- `"project": "."` was evaluated and not adopted. FastMCP's `UVEnvironment`
  composes `project` and `dependencies` (`uv run --project <path> --with ...`),
  so it would not conflict and would resolve the import issue above, but it
  changes the execution strategy from an ephemeral `--with` environment to a
  full project sync, and `fastmcp run fastmcp.json` could not be exercised end
  to end here. Adopted in D25 once the end-to-end check was possible.
- The D7 comment in `http_app` now gives the real reasons for keeping this
  project's DNS-rebinding middleware (built-in guard off by default, no SSE
  coverage, no `/health` exemption, no `X-Forwarded-Host` or trusted-proxy
  support) instead of the status-code argument. A comment at `run_query`
  registration records that FastMCP's per-tool `timeout=` is deliberately not
  used because it abandons the running query, whereas
  `CLICKHOUSE_MCP_QUERY_TIMEOUT` issues `KILL QUERY`.

## D19. Exact pin `fastmcp==4.0.1` (user decision, 2026-09-02)

- FastMCP's policy permits breaking changes in minor versions and calls open
  ranges bad practice. The user chose an exact pin over a `<4.1.0` range. The
  pin is the latest 4.x release at the time of the decision (4.0.1, released
  2026-09-02). `uv add "fastmcp==4.0.1"` updated `fastmcp` and `fastmcp-slim`
  in the lockfile; the full suite passes on 4.0.1 (562 tests, deprecation
  warnings as errors).
- Consequence: picking up any FastMCP fix requires a release of this package.
  Bumping the pin is a one-line change plus a full suite run. A scheduled CI
  job that installs the latest 4.x and runs the suite is still worth adding so
  the maintainers learn about upstream changes early.

## D20. SSE transport is removed (user decision, 2026-09-02)

- The SSE transport goes away in this release rather than being deprecated
  first. FastMCP 4 marks SSE `legacy_only`; SSE is permanently handshake-era,
  so the session-scoped context-state leak (D9) can never be fully closed
  there; and the FastMCP 4 jump is already a breaking release. If a user needs
  it back, the fallback is to reinstate it as deprecated, not to keep it as a
  supported transport.
- Scope of the removal: `TransportType.SSE` and the `sse` value of
  `CLICKHOUSE_MCP_SERVER_TRANSPORT` in `mcp_env.py` (reject the value with a
  clear migration error naming `http`); the SSE branch in `main.py`; the
  `transport="sse"` handling in `ClickHouseFastMCP.http_app`; every
  `transport="sse"` parametrization in tests (mainly
  `tests/test_http_security_boundary.py`, also `test_health_endpoint.py`,
  `test_http_security.py`, `test_context_config_override.py`); the README
  transport, auth, and middleware sections; the D7 comment that names SSE
  coverage as a reason for the custom DNS-rebinding middleware (still true for
  the other three reasons); the CHANGELOG breaking entry; and `server.json` if
  it lists the transport. Open item F5 (SSE session-leak regression test)
  closes as moot.

## D21. Public Python API: sync helpers supported, internals deprecated

- `mcp_clickhouse.__all__` keeps `list_databases`, `list_tables`, `run_query`,
  `run_chdb_select_query` (when chDB is enabled), and
  `create_clickhouse_client` as the supported synchronous API.
- `table_pagination_cache`, `fetch_table_names_from_system`,
  `get_paginated_table_data`, and `create_page_token` are internals. They stay
  exported in this release with a CHANGELOG deprecation notice and are removed
  from `__all__` in the next minor. The user accepted this recommendation
  without a strong preference, so a reviewer who prefers removing them now,
  given the release is already breaking, may do so.

## D22. `output_schema=None` and write-as-destructive confirmed

- The user confirmed D15 (`output_schema=None` on every tool, JSON-string text
  results only) and D14 as amended after review (`destructive_hint=True`
  whenever `CLICKHOUSE_ALLOW_WRITE_ACCESS=true`, independent of
  `CLICKHOUSE_ALLOW_DROP`). Both are final for this release.

## D23. Single PR: queues A, B, and C; version numbers deferred

- Queue A (contract, tests, packaging) is complete. Queue B (migration-specific
  tests) and Queue C (test hygiene: conftest, fixtures, asyncio mode,
  wall-clock assertions, CI matrix) are in scope for the same PR rather than a
  follow-up. The SSE removal (D20) and the API deprecation notice (D21) are
  also in scope.
- Version numbers in `pyproject.toml` and `server.json` are left alone (D8
  stands). Whoever cuts the release decides the number; the FastMCP jump and
  SSE removal make it a breaking release.

## D24. SSE is rejected in three places (implements D20)

- `TransportType.SSE` is gone. `mcp_env.REMOVED_SSE_TRANSPORT = "sse"` is the
  one place the string lives. `MCPServerConfig.server_transport` rejects it
  (case-insensitively) with a dedicated migration message naming
  `CLICKHOUSE_MCP_SERVER_TRANSPORT=http` and a streamable HTTP client; other
  unknown values keep the generic message, which now lists only `stdio` and
  `http`.
- `ClickHouseFastMCP.http_app` rejects `transport="sse"` (keyword or
  positional) before `_resolve_auth` runs, so no launch path, including
  `fastmcp run --transport sse`, can build an SSE app. `_resolve_auth` rejects
  it as well rather than returning `{}`, so an HTTP-family transport can never
  resolve to "no auth". Both use one module-level message.
- The `/sse` and `/messages/` endpoints are gone with the transport. The
  boundary tests keep every streamable HTTP assertion the removed SSE cases
  also made; positional-transport detection now uses `"streamable-http"`.
- Open item F5 (an SSE-specific session-leak regression test) closes as moot.
- Released CHANGELOG entries that mention SSE are history and were not edited.

## D25. `fastmcp.json` sets `environment.project` and `deployment.cwd`

- With only `environment.dependencies`, `fastmcp inspect fastmcp.json` from an
  unrelated directory failed twice over: the relative `source.path` resolves
  against the process working directory (FastMCP only resolves
  `deployment.cwd` against the config file), and the ephemeral `uv run --with`
  environment neither contains `fastmcp` nor installs `mcp_clickhouse`.
- `"project": "."` makes the CLI run `uv run --project <repo> --with ...`, which
  syncs the checkout and installs the package. Because `project` is resolved
  against the working directory, `"deployment": {"cwd": "."}` is set as well;
  the CLI applies it before building the uv command. Verified end to end with
  `fastmcp inspect --format mcp` from `/tmp` against the committed manifest.
- The `--with` dependency list is now redundant with the project sync but is
  kept, and `tests/test_packaging_metadata.py` still keeps it in sync with
  pyproject and now asserts `project` and `cwd`.
- Consequence: `fastmcp run fastmcp.json` from a checkout loads the
  repository's `.env` because the process working directory is the repository
  root. That matches `python -m mcp_clickhouse.main` run from the root.

## D26. Deprecated internals warn through a module `__getattr__` (implements D21)

- `mcp_clickhouse/__init__.py` no longer imports `table_pagination_cache`,
  `fetch_table_names_from_system`, `get_paginated_table_data`, or
  `create_page_token` directly. A module `__getattr__` emits a
  `DeprecationWarning` naming the attribute and returns the `mcp_server`
  object, so `mcp_clickhouse.create_page_token is mcp_server.create_page_token`
  still holds. Unknown attributes raise `AttributeError` as before.
- The four names stay in `__all__` this release (`from mcp_clickhouse import *`
  therefore warns once per name, which is the intended signal) and leave
  `__all__` in the next minor release. `tests/test_public_api.py` pins the
  warning, the identity, the silence of the supported API, and the `__all__`
  membership so the removal is a deliberate edit.
- Nothing inside the package imports these names from the package namespace;
  `tests/test_pagination.py` now imports them from `mcp_clickhouse.mcp_server`
  so the suite stays clean under `-W error::DeprecationWarning`.

## D27. Queue B and CI calls

- CI runs the suite on Python 3.10 and 3.13 (`fail-fast: false`); ruff runs
  once, on the 3.13 leg. A weekly `fastmcp-latest` job (also
  `workflow_dispatch`) syncs the pinned environment, runs
  `uv pip install --upgrade "fastmcp>=4,<5"`, and runs the suite with
  `uv run --no-sync` so uv does not restore the pin. It is gated off
  `pull_request` and `push`, and the regular jobs are gated off `schedule`, so
  it can never block a PR (D19).
- A `fastmcp inspect --format mcp` snapshot in CI was considered and not
  adopted: `tests/test_tool_contract.py` already asserts the annotations,
  schemas, titles, and output-schema suppression through `Client.list_tools()`,
  and a whole-document snapshot would churn on every FastMCP change without
  saying which contract moved.
- `example_middleware.py` gains `ClientConfigOverrideMiddleware`, the README
  pattern as runnable code, registered fourth by its `setup_middleware`. Tests
  load the module through the `MCP_MIDDLEWARE_MODULE` hook on a fresh
  `FastMCP("t")`, never the singleton, and spy on `Context.set_state` to pin
  `serializable=False`.
- Protocol negotiation is pinned against the real app: 2025-03-26, 2025-06-18,
  and 2025-11-25 are echoed; 2026-07-28 is downgraded to 2025-11-25 over a
  plain `initialize` POST and still gets an `mcp-session-id`, so every session
  this server issues is handshake-era and D9 stays load bearing.
  `stateless_http=True` removes the session id and leaves the 421 and 401
  gates intact. `fastmcp.settings.http_host_origin_protection` is pinned at
  `False` and `HostOriginGuardMiddleware` is asserted absent from the app.
- The metadata-tool responsiveness tests are event-gated: the sync helper is
  parked on a `threading.Event` inside `QUERY_EXECUTOR` while a concurrent
  `list_tools` must complete. `Client.ping()` is not implemented by this
  server's in-memory transport, so `list_tools` is the probe.

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
propagation, and caller cancellation of a queued future. `list_tables` stops
its worker at the deadline, with tests for the page query and the per-table
column loop. `mcp_env.py` describes the variable as the
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

## Second review round (after the R1-R8 fixes)

An adversarial review of commits 1792053, 96371f0, and aad10de found eight
items. All are addressed in commit 86b2d92 except where noted.

- F1 (medium, confirmed). Deleting the key after capture also removed the
  request-scoped copy, so a retry within one request (FastMCP `RetryMiddleware`)
  ran on the base configuration and returned success. Fixed by restoring a
  request-scoped copy after the delete (D9 step 2); regression test through
  `fastmcp.Client` with `RetryMiddleware`.
- F2 (medium, confirmed). Validating before deleting left an invalid
  session-scoped value in place, failing every later call in the session. Fixed
  by deleting first (D9 step 1); regression test over real streamable HTTP.
- F3 (medium, confirmed). The metadata timeout docstring understated the worker
  hold because `list_tables` runs `page_size + 2` sequential queries. Fixed with
  the cooperative deadline (D11).
- F4 (medium, confirmed). The cancellation test did not exercise the
  `CancelledError` branch, which was redundant. Branch removed, test rewritten
  to saturate the executor and assert the queued future is cancelled (D11).
- F5 (low, confirmed). The leak also affects SSE. Docs now say "HTTP session
  (streamable HTTP or SSE)". Open: no SSE-specific regression test; it would
  need a live uvicorn server.
- F6 (low, confirmed). The wrong-return-type message did not name the env var,
  and D12 misstated the import path. Both corrected.
- F7 (low). The lock test only proved acquisition. Rewritten as an exclusion
  test with the real upstream signature via `functools.wraps`.
- F8 (low). AGENTS.md now states the `serializable=False` requirement.

Verified clean by the reviewer: `delete_state` safety on stdio, in-memory
client, streamable HTTP with and without a session, and SSE; no key mismatch
between middleware and tool contexts; no interference with FastMCP visibility
state; `http_app` does not mutate the shared `mcp` instance; explicit
`description=` changes only the description in `list_tools()`; the
`run_http_async` signature test is meaningful.

## Remaining open items

- SSE removal (D20), not started. Closes F5 as moot.
- CHANGELOG deprecation notice for the internal exports (D21), not written.
- Queue B and Queue C from the handover document (D23), not started.
- README chDB section does not document the `{"status": "error"}` payload
  shape (D13 follow-up).
- `fastmcp.json` could use `"project": "."` (or `"editable": ["."]`) so
  `fastmcp run fastmcp.json` works outside the repository root; the
  hand-maintained dependency list does not fix the `mcp_clickhouse` import
  (D18). Needs an end-to-end `fastmcp run` check first.
- Scheduled CI job installing the latest FastMCP 4.x (D19), not added.
- Resolved: `tests/test_mcp_server.py::test_system_database_access` failed on
  ClickHouse 26.8 because it requested `page_size=100` and the `system` database
  now holds 139 tables, so `tables` (alphabetical position 124) fell on the
  second page. The test now follows `next_page_token` until it is null, asserts
  the collected count equals `total_tables`, and checks the expected names in
  the union, so it is independent of the ClickHouse version.
- A stale worktree from an earlier session sits at
  `.claude/worktrees/agent-a688a0c4ef7bbe86d` with uncommitted `pyproject.toml`
  and `uv.lock` edits. It is untracked and was left alone.
