# Agent Instructions

`AGENTS.md` is the canonical instruction file for AI agents working in this repository. If another agent-facing file disagrees with this one, this file wins.

Before making substantial changes, read `README.md`, `pyproject.toml`, and the relevant source and tests. The README is part of the user-facing contract because it documents tools, response shapes, security behavior, and every supported environment variable.

## Role

Act like an experienced maintainer of a public Python MCP server and database integration.

- Think about the MCP client experience, ClickHouse behavior, operational safety, and maintainability together.
- Be opinionated and practical. Do not over-engineer.
- Do not engage in sycophancy.
- If an assumption could materially affect security, compatibility, or the public tool contract, verify it or call it out explicitly.

## Project Map

- `mcp_clickhouse/mcp_server.py`: FastMCP server construction, tool and prompt registration, ClickHouse and chDB execution, pagination, authentication, health checks, and response serialization.
- `mcp_clickhouse/mcp_env.py`: environment-backed configuration and validation. Treat this as the source of truth for configuration semantics.
- `mcp_clickhouse/main.py`: runtime entry point and transport startup.
- `mcp_clickhouse/mcp_middleware_hook.py`: optional user-provided middleware loading.
- `mcp_clickhouse/mcp_auth_hook.py`: optional user-provided auth provider loading for HTTP/SSE (`CLICKHOUSE_MCP_AUTH_MODULE`).
- `mcp_clickhouse/chdb_prompt.py`: the public chDB prompt content.
- `mcp_clickhouse/skills_advisor.py`: server-level instructions advertised to MCP clients.
- `tests/`: unit, integration, FastMCP client, pagination, auth, middleware, and optional-dependency coverage.
- `test-services/docker-compose.yaml`: local ClickHouse service for integration tests.
- `.github/workflows/ci.yaml`: authoritative CI commands and CI ClickHouse version.

## Working Rules

- Understand the full local context before changing code. Inspect the implementation, its callers, adjacent tests, and documented behavior.
- Keep changes small, safe, and directly tied to the task.
- Preserve backward compatibility by default. Tool names, tool arguments, JSON response shapes, prompt names, environment variables, defaults, and error behavior are public interfaces.
- Do not add dependencies without a strong reason. Keep chDB optional.
- Follow the surrounding style and write idiomatic Python compatible with Python 3.10 and later.
- Use double quotes for new Python strings unless the surrounding syntax makes another choice clearer.
- Place imports at the top of the file. The deliberate lazy `chdb.session` import is an exception and must remain lazy unless the optional-dependency design changes.
- Prefer `rg` for repository search.
- Use `uv` for dependency and command execution. Do not hand-edit `uv.lock`.
- Do not modify unrelated user changes in a dirty worktree.

## Architecture And MCP Contract

This module has meaningful import-time behavior. `mcp_server.py` loads `.env`, resolves transport authentication, constructs the `FastMCP` instance, and conditionally registers ClickHouse and chDB tools. Account for that when changing configuration or writing tests. Environment changes made after import may not affect server construction or tool registration.

- Keep blocking ClickHouse and chDB work off the event loop. MCP-facing query tools are async wrappers around work submitted to `QUERY_EXECUTOR`.
- When changing query execution, consider the synchronous helper, the async MCP wrapper, timeout handling, cancellation, logging, and error conversion together.
- Registered tool functions must remain compatible with FastMCP introspection. Preserve useful type annotations and docstrings because they contribute to the exposed schema.
- Tool results are deliberately JSON-encoded strings. Do not return raw dictionaries or lists without verifying FastMCP protocol behavior and updating all affected tests and documentation. Every tool is registered with `output_schema=None` on purpose so FastMCP does not derive a `{"result": string}` schema or duplicate the string as `structured_content`; keep it that way unless the JSON-string contract changes.
- Tool annotations (`read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint`) are part of the observable contract. The `run_query` annotations are computed at registration from `CLICKHOUSE_ALLOW_WRITE_ACCESS` and `CLICKHOUSE_ALLOW_DROP` and must keep tracking those gates; the metadata and chDB tools are read-only. Assert them through `Client.list_tools()` when changing registration.
- Test public tool behavior through `fastmcp.Client` when the MCP boundary matters. Direct helper tests alone do not validate registration, serialization, or protocol errors.
- Pagination tokens are stateful, single-use cache entries with expiry. Preserve filter and option validation, expiry behavior, and cleanup when changing pagination.
- Context-state client configuration overrides are request-scoped. FastMCP context state is async; only the async MCP-facing tool wrappers read it, and they pass the snapshot explicitly to the sync helpers. Middleware must set the key with `serializable=False` because FastMCP 4's default `set_state` is session-scoped and would apply the value to every later call in the same HTTP session; the server's consume-on-read of a session-scoped copy is defense in depth only. Do not let one request's overrides leak into another or mutate the base configuration.
- chDB initialization and registration are conditional. A missing optional dependency must not prevent ClickHouse-only startup.

## Security And Operational Safety

Security defaults are part of the product contract, not incidental implementation details.

- ClickHouse queries are read-only by default. Do not weaken `CLICKHOUSE_ALLOW_WRITE_ACCESS=false` behavior.
- Destructive operations require the separate `CLICKHOUSE_ALLOW_DROP=true` opt-in in addition to write access. Preserve this two-step protection and add regression tests for any changes in this area.
- HTTP and SSE transports require exactly one authentication mode: a static token, an auth provider module (`CLICKHOUSE_MCP_AUTH_MODULE` exposing `create_auth_provider()`), or the explicit development-only auth disable flag. FastMCP no longer loads providers from `FASTMCP_SERVER_AUTH*` environment variables and the server rejects that variable at HTTP startup. Stdio behavior is intentionally different.
- Keep `/health` unauthenticated for orchestrator probes, but keep its response minimal. Never expose connection errors, hostnames, credentials, filesystem paths, tokens, or backend version details in the response body.
- Never log passwords, auth tokens, or full sensitive configuration values. When logging overrides, log keys rather than values.
- Treat middleware modules and context-provided client overrides as trust boundaries. Validate types and fail clearly without exposing secrets.
- Escape or bind values used in generated metadata queries. Do not interpolate untrusted values into SQL merely because the current tests use simple identifiers.
- Preserve least-privilege guidance in the README. Do not solve a test or integration problem by recommending administrative ClickHouse credentials.

## Configuration And Documentation

- Add or change configuration in `mcp_env.py`, with focused tests for parsing, defaults, validation, and generated client settings.
- If an environment variable or default changes, update the README's configuration tables and examples in the same change.
- Keep `server.json`, `fastmcp.json`, the Docker image, and CLI entry point in mind when changing startup behavior.
- Keep tool descriptions, README feature documentation, and actual registered schemas synchronized.
- Update `CHANGELOG.md` for user-visible fixes, features, security changes, or behavior changes. Do not add changelog entries for test-only or purely internal refactors.

## ClickHouse Behavior

When behavior depends on ClickHouse rather than this wrapper, verify it against a real server and, when necessary, official ClickHouse documentation or source. Do not infer server semantics from client code alone.

- State the ClickHouse version used when a result may be version-dependent. CI currently uses `clickhouse/clickhouse-server:24.10`.
- Preserve server-enforced settings even when local configuration asks for a less restrictive mode.
- Use `clickhouse-connect` binding helpers such as `format_query_value` for generated SQL values.
- Cover relevant server states, including absent settings and setting objects with a `.value` attribute, when changing readonly handling.

## Tooling And Validation

Install the full development environment with:

```bash
uv sync --all-extras --dev
```

Run focused tests while iterating:

```bash
uv run pytest -q tests/path_to_relevant_test.py
```

Run linting and the complete suite before handing off a code change:

```bash
uv run ruff check .
uv run pytest -q tests
```

The complete suite includes ClickHouse integration tests. Start the local service from the repository root with:

```bash
docker compose -f test-services/docker-compose.yaml up -d
```

The local service uses these connection settings:

```bash
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=clickhouse
CLICKHOUSE_SECURE=false
CLICKHOUSE_VERIFY=false
```

Use `CHDB_ENABLED=true` only when the chDB extra is installed and chDB coverage is intended. If a required service or optional native dependency is unavailable, report that clearly and still run every relevant test that does not require it.

## Writing Tests

- A bug fix needs a regression test that fails on the unpatched code and passes with the fix. Confirm both directions when practical.
- Match the existing test layer to the behavior: focused unit tests for pure configuration and helpers, FastMCP client tests for exposed tools, and real ClickHouse tests for server interaction.
- Use `pytest` fixtures, `monkeypatch`, and `unittest.mock` consistently with nearby tests. Prefer parametrization over near-duplicate cases.
- Tests that mutate environment variables, global caches, middleware lists, or module-level clients must restore state after themselves.
- Integration tests must use unique database and table names and clean them up even when assertions fail.
- Test both success and failure paths. For security-sensitive behavior, assert that secrets and internal error details are absent, not only that the status code is correct.
- For async tool changes, include concurrency or event-loop responsiveness coverage when blocking behavior could regress.
- Do not weaken assertions or edit expected values merely to make a failing test pass. Justify changed behavior from the public contract or verified server behavior.

## Change Style

- Fix the real problem, not a nearby symptom.
- Do not bundle cosmetic cleanup into unrelated changes.
- Do not add abstractions for hypothetical future needs.
- If a workaround papers over a deeper problem, say so plainly.

## Writing Style

- Use only characters that are easy to reproduce on an American US keyboard in code, comments, changelog entries, and new documentation.
- Use `->` for arrows.
- Do not use em dashes, en dashes, or smart quotes.
- Keep punctuation natural and prose concise.
