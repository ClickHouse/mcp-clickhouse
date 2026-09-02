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

## D13. chDB tool errors stay a successful result (superseded by D30)

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
- Limitation (D29): the CLI applies `deployment.cwd` before it re-executes
  itself and passes the original manifest path to the child, so a relative
  path such as `fastmcp inspect ../mcp-clickhouse/fastmcp.json` fails with
  "File not found" while an absolute path works. The CHANGELOG says to pass an
  absolute path.

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
- A module `__dir__` keeps the four names visible to `dir()`, `help()`, and
  `inspect.getmembers()` while they are exported (D29). Because they remain in
  `__all__`, `from mcp_clickhouse import *` warns once per name and therefore
  fails for downstream projects that run with `-W error::DeprecationWarning`;
  the CHANGELOG says so. Removing them from `__all__` now would avoid that but
  contradict D21's timeline, so the timeline stands.

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
  pattern as runnable code. It is deliberately not registered by the example's
  `setup_middleware` (D29): doing so would replace the configured ClickHouse
  timeouts on every tool call for anyone who enables the documented example
  module. Tests load the module through the `MCP_MIDDLEWARE_MODULE` hook on a
  fresh `FastMCP("t")`, never the singleton, assert the template is not
  registered, exercise it directly, and spy on `Context.set_state` to pin
  `serializable=False`.
- Protocol negotiation is pinned against the real app: 2025-03-26, 2025-06-18,
  and 2025-11-25 are echoed. The MCP SDK selects the sessionless 2026-07-28
  wire from the `MCP-Protocol-Version` request header, not from the
  `initialize` body, so an `initialize` asking for 2026-07-28 without that
  header is served by the handshake-era transport, downgraded to 2025-11-25,
  and given an `mcp-session-id`. Every handshake therefore ends in a
  session-scoped store and D9 stays load bearing for handshake clients. The
  sessionless wire is covered separately (D29): a header-routed `tools/list`
  gets 200 with no session id and the 2026-07-28 result envelope, and the
  401, 421, and 403 gates hold in front of it. `stateless_http=True` removes
  the session id and leaves the gates intact.
  `fastmcp.settings.http_host_origin_protection` is pinned at `False` and
  `HostOriginGuardMiddleware` is asserted absent from the app.
- The metadata-tool responsiveness tests are event-gated: the sync helper is
  parked on a `threading.Event` inside `QUERY_EXECUTOR` while a concurrent
  `list_tools` must complete. `Client.ping()` is not implemented by this
  server's in-memory transport, so `list_tools` is the probe.

- Tool-error wire shape, verified over real streamable HTTP: a timeout
  `ToolError` and a `list_tables` validation failure both arrive as a JSON-RPC
  result with `isError: true`, never a JSON-RPC error object. FastMCP 4's
  `to_mcp_error` (INTERNAL_ERROR -32603, INVALID_PARAMS -32602) is only used
  for requests that never reach a tool. The handover's "JSON-RPC error code
  for a timeout" item therefore pins the result shape instead; the timeout
  text is also asserted not to contain the ClickHouse host or "password".
- `server.json` is not edited. Tests pin what holds today (both package
  entries declare the same variables, every declared variable is documented
  in the README and read in `mcp_env.py`, versions parse as PEP 440) and two
  `xfail(strict=True)` tests record the known gaps so fixing either forces the
  marker out: version equality with pyproject (0.4.0 vs 0.5.0, release prep
  decides) and the seventeen README-documented variables that server.json
  omits (`CHDB_*`, `CLICKHOUSE_ENABLED`, the timeouts, `CLICKHOUSE_PROXY_PATH`,
  `CLICKHOUSE_SERVER_HOST_NAME`, every `CLICKHOUSE_MCP_*` transport and auth
  variable, `MCP_MIDDLEWARE_MODULE`). Whether the registry manifest should
  list HTTP-only variables for stdio packages is a release-prep call.

## D28. Queue C test hygiene

- `tests/conftest.py` holds fixtures only: the shared `mcp_server` fixture
  (the module singleton), an autouse `reset_server_state` that runs
  `_clear_client_cache()` (closing cached clients), clears `_active_queries`
  under its lock and `table_pagination_cache`, and resets
  `_grants_advisory_done` before and after every test, plus `clean_http_env`
  and `authenticated_app_env`. Nothing in the suite relied on cross-test
  state; five `setup_method`/`teardown_method` pairs in the client-cache tests,
  three in cancellation, three in context-override, one autouse fixture in
  connection-errors, and the wire tests' pairs became redundant and were
  removed.
- Constants, classes, and plain functions used at 70+ call sites live in an
  importable `tests/helpers.py` (with an empty `tests/__init__.py`) rather
  than fixtures, because `from tests.helpers import fake_clickhouse_client`
  reads better than a fixture parameter at that many sites:
  `fake_clickhouse_client` (37 former `MagicMock(server_version=...)` sites),
  `HTTP_ENV_VARS`/`clear_http_env` (the union of four scrub lists; the
  auth-config tests now also clear bind and allow-list variables they never
  read), `MCP_HEADERS`, `INITIALIZE_REQUEST`, `initialize_request(version)`,
  `jsonrpc_body`, `install_auth_module`, `static_token_provider`,
  `RecordingApp`, `send_asgi_request(_async)`. The initialize request is
  standardized on protocol 2025-11-25 (the context-override HTTP session tests
  moved from 2025-06-18 and still observe the leak).
- The two incompatible `_install_auth_module` helpers are resolved: the
  auth-config tests use the shared installer; the boundary tests keep a
  three-line `_install_static_token_auth_module` that also sets
  `CLICKHOUSE_MCP_AUTH_MODULE`.
- `[tool.pytest.ini_options]`: `testpaths = ["tests"]`, `asyncio_mode = "auto"`,
  `asyncio_default_fixture_loop_scope = "function"` (pytest-asyncio 1.3.0),
  and `filterwarnings = ["error::DeprecationWarning"]`, so CI now enforces
  what the handover verified by hand. Existing `@pytest.mark.asyncio` marks
  are kept as documentation; every async test carries one. The hand-rolled
  module-scoped `event_loop` fixture in `tests/test_mcp_server.py` is gone and
  `setup_test_database` is a plain sync module-scoped fixture, which is what
  makes the function-scoped default loop safe.
- `FASTMCP_MCP_CAMELCASE_COMPAT=false` is applied by
  `os.environ.setdefault` at the top of `conftest.py`, before anything imports
  fastmcp (its settings object is built at import). A test asserts
  `fastmcp.settings.mcp_camelcase_compat is False`. CI keeps the explicit
  variable; it is now redundant.
- Wall-clock assertions replaced by event gates: `run_query`'s
  does-not-block test parks `execute_query` on a `threading.Event` while
  `list_tools` must complete; the health-probe and cancellation off-loop tests
  wait for the worker's start event through `run_in_executor` and then assert
  `not task.done()`, which can only hold if the blocking call ran off the
  loop. `test_pathological_detach_input_is_fast` keeps its generous
  regex-runtime guard; bounded `asyncio.sleep(0.01)` polling loops are retries,
  not assertions.
- pytest-randomly is not added: the suite is verified identical in forward,
  reverse-file, and per-file-isolated order, and that is not worth a lockfile
  change.
- Counts: 562 tests at the start of this tranche's second session, 609 at the
  end (607 passed, 2 strict xfails) after the D29 review additions. No test
  was removed in queue C; the SSE removal dropped 11 cases and added 10.

## D29. Third review round (everything since 61837a7)

An adversarial read-only review of the SSE removal, deprecation shim,
fastmcp.json change, queue B tests, CI, and queue C hygiene found one high,
three medium, and ten low items. All confirmed items are fixed except where
noted.

- High, fixed: the new Python 3.13 CI leg could never run. `uv run` treats the
  committed `.python-version` (3.10) as an interpreter request, so after
  `uv sync --python 3.13` it recreated `.venv` at 3.10 without the dev extras
  and could not find pytest; ruff, gated on that leg, never ran either. Both
  jobs now set `UV_PYTHON` at job level, which outranks the file for every uv
  command (verified in a scratch project: sync and run both stay on 3.13).
  Every job has `timeout-minutes: 30`.
- Medium, fixed: the two event-gated responsiveness tests hung forever under
  the regression they guard, because nothing on a blocked event loop can set
  the release event. The parked fake helper now gives up after five seconds
  and records that it did; the test asserts it never had to. Under an inline
  executor the tests fail in about ten seconds. The health-probe and
  cancellation off-loop tests already bounded their wait.
- Medium, fixed: `tests/test_http_transport_contract.py` claimed FastMCP 4
  "does not recognize" 2026-07-28. It does; the SDK routes to the sessionless
  wire from the `MCP-Protocol-Version` header, and the branch had no test on
  that wire at all. Added `TestSessionlessWire` (see D27). The security gates
  on that wire were verified clean by the reviewer and are now pinned.
- Medium, fixed: registering `ClientConfigOverrideMiddleware` from the
  example's `setup_middleware` silently forced `connect_timeout=60` and
  `send_receive_timeout=120` on every call for anyone following the README's
  `MCP_MIDDLEWARE_MODULE=example_middleware` instructions. It is a template
  now, not registered; README and tests updated. No CHANGELOG entry because
  the documented behavior of the example did not change.
- Low, fixed: the weekly job ran on 3.10 for the same `.python-version`
  reason (UV_PYTHON); `__dir__` added to the package; the CHANGELOG
  deprecation entry warns about star imports and the Fixed entry says to pass
  `fastmcp.json` as an absolute path; `### Deprecated` now precedes
  `### Removed`; `_resolve_auth` and `http_app` reject `sse` in any letter
  case, matching `mcp_env`; `tests/test_server_json.py` reads the version from
  `pyproject.toml` rather than the installed distribution; two vacuous
  host/password assertions left the wire test; the three README pitfalls
  bullets are consistent.
- Low, not changed, needs a repository setting: the CI check is now named
  `test (python 3.10)` / `test (python 3.13)` instead of `test`. A branch
  protection rule requiring the `test` context will block merges until it is
  updated to the new names. Whoever opens the PR should check.
- Noted, unverifiable locally: `filterwarnings = error::DeprecationWarning`
  will now run on 3.13 in CI for the first time. The reviewer grepped the
  installed dependencies for the usual 3.12/3.13 offenders
  (`datetime.utcnow`, `pkg_resources`, bare `asyncio.get_event_loop`) and
  found none, so the risk is low. The Dockerfile builds from a 3.13 uv image
  but copies `.python-version` (3.10) before its second sync, so the shipped
  image is probably 3.10; pre-existing and untouched.
- Verified clean by the reviewer: every launch path for `sse` fails with the
  migration message before any side effect (`run_async` -> `run_http_async`
  -> the subclass `http_app` is the only route); no HTTP-family transport
  resolves to no auth or skips the Host/Origin middleware; the context-state
  leak tests still fail on pre-D9 code after moving to protocol 2025-11-25;
  the conftest autouse reset masks nothing; the `Context.set_state` spy runs
  the real implementation; `--with` and `--project` coexist in the generated
  uv command; no async fixtures exist so the fixture loop scope is inert; the
  README anchors resolve; no em dash, en dash, or smart quote was introduced.

## D30. Acceptance-test round: chDB errors, mode-aware descriptions, chDB annotations

A black-box acceptance test (a fresh Claude Code session driving the built wheel
on Python 3.13 through eight MCP server configurations against ClickHouse 26.9
and 24.10, over stdio and streamable HTTP; workspace `/tmp/mcp-clickhouse-uat`,
report `REPORT.md` there) produced five failures and a list of confusing
behaviors. Three items were introduced by or made cheap by this branch and are
fixed here; the rest are pre-existing and deferred (D31).

- `run_chdb_select_query` failures are MCP tool errors (user decision,
  2026-09-02, reversing D13). Engine errors raise
  `ToolError("chDB query failed: <message>")`, timeouts raise
  `ToolError("chDB query timed out after N seconds")`, and unexpected exceptions
  raise `ToolError("chDB query failed: <message>")`, in both the async wrapper
  and the exported sync helper. The `{"status": "error"}` payload is gone. The
  user accepted the break because the release is already breaking and the
  tester independently found the old shape inconsistent with `run_query`.
  Result rows keep their pre-existing list-of-objects shape.
- The `run_query` description is computed at registration from the same gates
  as the annotations and states the mode: read-only (including that a
  `SETTINGS` clause is refused under `readonly=1`), writes without destructive
  statements, or writes including them. It names the response shape and still
  names the operator variable that changes the mode. The tester's strongest
  usability finding was that the three configurations were indistinguishable
  from the tool list. The incomplete-configuration fallback uses the read-only
  text, matching the annotations fallback.
- chDB annotations follow `CHDB_DATA_PATH`: `:memory:` keeps the read-only,
  idempotent hints; a filesystem path advertises `read_only_hint=False`,
  `destructive_hint=True`, `idempotent_hint=False`, because the tool runs any
  SQL and the results persist. The description says which case applies without
  printing the path.
- The `query` parameter of both query tools gains a description through an
  `Args:` block on the MCP-facing wrappers; it was the only undocumented
  parameter, and FastMCP 4's docstring parser made the fix a docstring edit.
- A second tester run on the rebuilt wheel confirmed all four fixes (the
  mode-aware description was called out as the reason the write-mode
  configurations are now distinguishable; the chDB tool error was called
  consistent with `run_query`). It also found that release 0.5.0 already
  emitted an output schema and `structuredContent: {"result": "<json>"}`,
  which D15 removes; the CHANGELOG entry now states that break instead of
  saying "as before". Two description nits from that run were fixed:
  `list_tables` promised a "column count" field that does not exist, and the
  write-mode `run_query` descriptions now say every statement is subject to
  the ClickHouse user's grants (the `reader` configuration had made the
  description over-promise).
- Verified clean by the tester and not changed: HTTP auth, Host, and Origin
  gates over a real uvicorn process; the write and drop gates including the
  string-literal and comment edge cases; `list_tables` metadata parity between
  ClickHouse 26.9 and 24.10; large integers as strings; the 30 second timeout
  followed by server-side cancellation; the branch versus the PyPI 0.5.0 release
  (parameter descriptions, titles, annotations, unwrapped JSON, correct
  `serverInfo.version`).

## D31. Deferred findings from the acceptance test (deliberately not fixed here)

Every item below exists on `main` today; the code paths are identical there.
They are recorded so they can be fixed in a follow-up, or picked up now if the
user chooses. None blocks the FastMCP 4 PR.

1. `list_tables` accepts an unknown, expired, or already consumed `page_token`
   and silently restarts at page 1 with a fresh token (`_list_tables_impl`
   falls through when the token is not in `table_pagination_cache`, and also
   when the cached filters differ from the request). A client that reuses a
   token gets duplicate tables without any signal. Proposed fix: raise
   `ToolError("Unknown or expired page_token")` in both cases; the filter
   mismatch currently only logs.
2. A `run_query` result with zero rows returns `{"columns": [], "rows": []}`.
   clickhouse-connect's Native-format result carries no column names when the
   result set is empty, so the schema is lost. Proposed fix: recover the
   column list (for example from `result.column_names` after a
   `WITH TOTALS`-free header, or by falling back to a `DESCRIBE` of the query)
   and pin it with a test.
3. Read-only refusals surface as raw ClickHouse exceptions: `INSERT` into a
   missing table fails with `UNKNOWN_TABLE`, and `CREATE`/`DROP` with
   `default: Cannot execute query in readonly mode. (READONLY)`, so a new user
   blames the database account rather than the MCP setting. Proposed fix: when
   the server itself applied `readonly=1`, wrap ClickHouse error code 164 with
   "This MCP server runs in read-only mode; operators enable writes with
   CLICKHOUSE_ALLOW_WRITE_ACCESS=true", and leave code 497 (ACCESS_DENIED,
   the database user's grants) untouched. The mode-aware description (D30)
   softens this in the meantime.
4. `INSERT` returns `{"columns": [], "rows": []}` while DDL statements return
   a statistics row (`read_rows`, `written_rows`, `elapsed_ns`, `query_id`,
   mostly zeros). The one statement where `written_rows` is informative
   reports nothing. Proposed fix: run non-SELECT statements through
   `client.command` and return one normalized summary shape.
5. ClickHouse error text passed to clients ends with
   `(for url http://<host>:<port>)`, exposing the internal ClickHouse address,
   and echoes ` FORMAT Native`, which clickhouse-connect appends. Proposed fix:
   strip the `(for url ...)` suffix in `execute_query`'s error conversion.
6. `Query timed out after N seconds` does not say the query was cancelled on
   the server (it is, via `KILL QUERY`) or name `CLICKHOUSE_MCP_QUERY_TIMEOUT`.
   Proposed fix: extend the message.
7. Documentation gaps: Decimal values arrive as JSON strings while floats are
   numbers, and `DateTime64` loses sub-second precision in the text form (the
   README only warns about large integers); the `list_tables` description
   promises a "column count" field that does not exist (`columns` is an array
   and `total_rows`/`total_bytes` are `null` for views); the chDB prompt reads
   as a system prompt for a specific chat product, with emoji headings and a
   Python "analysis tool" fallback that calls the MCP tool as a function, and
   never says that in-memory state is lost on restart.
8. Not defects, recorded for completeness: tool annotations describe the MCP
   configuration, not the database user's effective grants (D14, D22); the
   HTTP 401 has an empty body but does carry `WWW-Authenticate: Bearer`; the
   Agent Skills line in the server instructions is the 0.4.1 feature; prompts
   and annotations are invisible in the Claude Code client, which is a client
   limitation.
9. Added by the second run: `DateTime64` text drops the fraction when it is
   zero and keeps it otherwise (`2025-01-01 00:00:00+00:00` next to
   `2025-01-01 00:00:00.123000+00:00` in one column), and the offset is the
   ClickHouse server's timezone (`+01:00` on the 24.10 fixture), so the same
   data renders differently on two servers; a single UInt64 column mixes JSON
   numbers and strings around 2^53 (documented, still a footgun); the
   `INSERT` into a missing table under read-only fails with `UNKNOWN_TABLE`
   because ClickHouse resolves the table first, so the server could pre-empt
   with its own read-only message for non-read statements when the write gate
   is off (belongs with item 3); `columns[].database` and `columns[].table`
   repeat the parent table on every column; the `page_size` validation error
   is pydantic's text with an errors.pydantic.dev link; ClickHouse syntax
   errors relay the full "Expected one of" list (about 2 KB); `open_world_hint`
   is True on every tool although each talks to one configured database (D14
   chose True because the database is external; debatable, left alone);
   `serverInfo.version` is 0.5.0 on both builds until release prep bumps it.

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

- Release prep (not this branch): choose the version number (breaking:
  FastMCP 4, SSE removal), sync `server.json` to it and decide whether it
  should list the HTTP-only variables; the two strict xfails in
  `tests/test_server_json.py` flag both.
- Before merging: update any branch protection rule that requires the `test`
  check to the matrix names `test (python 3.10)` and `test (python 3.13)`
  (D29).
- Follow-up PR candidates from the acceptance test: the eight deferred items
  in D31. The user may choose to fix some of them before release.
- Next minor: remove the four deprecated pagination internals from
  `mcp_clickhouse.__all__` and `__getattr__` (D21, D26);
  `tests/test_public_api.py` pins the current state so the removal is a
  deliberate edit.
- If an SSE user objects, reinstate the transport as deprecated rather than
  supported (D20).
- Later, optional, assessed and not adopted this round: argument completion
  for `list_tables`, progress notifications in the `list_tables` deadline
  loop, per-tool auth scopes, schema resources, response caching middleware.
- A stale worktree from an earlier session sits at
  `.claude/worktrees/agent-a688a0c4ef7bbe86d` with uncommitted `pyproject.toml`
  and `uv.lock` edits. It is untracked and was left alone.
