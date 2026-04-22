# AI Review Guide

This document is for AI-assisted code review, patch review, and PR analysis in this repository.

Read `AGENTS.md` first. If the review touches substantive code paths, read `.agents/architecture.md` before reviewing.

## Review Priorities

Prioritize findings in this order:

1. Correctness bugs
2. Safety regressions (read-only enforcement, destructive-op gating, HTTP/SSE authentication)
3. Regressions in observable behavior (tool names, tool argument shapes, tool return shapes, prompt names, environment variable semantics, `/health` contract)
4. Public API and compatibility risk
5. Backend-enablement regressions across the ClickHouse and chDB combinations
6. Packaging and optional dependency regressions, especially around the `chdb` extra
7. Resource and timeout behavior (thread pool, query timeout, pagination cache)
8. Missing or weak tests
9. Style and nits

## Repo-Specific Review Checklist

When reviewing a change, explicitly check whether it affects:

- tool names, argument names, argument defaults, or return shapes
- prompt names or prompt registration
- environment variable names, defaults, or semantics (update `README.md` if so)
- read-only enforcement in `get_readonly_setting` and `build_query_settings`
- destructive-op detection in `_validate_query_for_destructive_ops` (regex breadth, case handling, newline handling, new DDL forms)
- HTTP and SSE authentication wiring and the token-or-disabled invariant
- `/health` response contract across the four backend-enablement combinations (ClickHouse on or off, chDB on or off)
- behavior when the `chdb` extra is missing. The server should warn and skip registration, not crash
- config singletons and whether a code path mutates env vars without resetting the singleton
- pagination token lifecycle (creation, reuse across filter changes, TTL eviction)
- query thread pool behavior and the `CLICKHOUSE_MCP_QUERY_TIMEOUT` contract
- context-based client config overrides in `create_clickhouse_client` and non-dict handling
- middleware loader behavior, including error handling and the `setup_middleware` contract
- compatibility expectations for Python, ClickHouse, `fastmcp`, and `clickhouse-connect` versions covered in CI

For tool-surface changes, confirm the tests exercise the behavior through the MCP client (`fastmcp.Client`) and not only the underlying function.

## What Good Review Feedback Looks Like

- Lead with findings, not summary.
- Order findings by severity.
- Use `file:line` references.
- Be explicit about impact.
- Call out what could break for real MCP clients and users (Claude Desktop configs, existing tool arguments, existing env vars).
- Distinguish confirmed issues from inferred risk.

If no material issues are found, say that explicitly and mention any residual testing or compatibility gaps (for example, chDB path not exercised, HTTP transport not smoke-tested, safety matrix not re-validated).

When a finding or assumption depends on library or server behavior that may have changed, verify it against the upstream source (ClickHouse, `clickhouse-connect`, `FastMCP`, `chDB`) rather than relying on memory. Note the version or commit you checked against. Flag any claim in the diff that looks version-sensitive and was not verified.

## Preferred Review Output

Use a structure like this:

1. Findings, ordered by severity
2. Open questions or assumptions
3. Brief change summary, only if useful

Each finding should answer:

- what is wrong
- why it matters
- who or what it could break
- what evidence in the diff or repo context supports the concern

These points should be brief but factual and accurate.

## Review Closing Checklist

Before saying a change looks good, make sure you understand:

- whether the MCP tool surface (names, arguments, returns) changed, intentionally or not
- whether any environment variable behavior changed and whether `README.md` reflects it
- whether safety defaults still hold: read-only by default, DROP gated behind `CLICKHOUSE_ALLOW_DROP`, HTTP/SSE auth required unless explicitly disabled
- whether the change holds up across the ClickHouse-on, ClickHouse-off, chDB-on, chDB-off matrix
- whether the `chdb` extra still being optional is respected
- whether transport behavior still works for `stdio`, `http`, and `sse`
- whether tests target the right layer (unit config tests with `monkeypatch`, tool tests through a live ClickHouse, or MCP-level tests via `fastmcp.Client`)
- whether any important validation still has not been run, for example a local server round trip or a container-image smoke test
