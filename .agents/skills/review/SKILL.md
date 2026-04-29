---
name: review
description: Use when reviewing pull requests, code changes, or patches in mcp-clickhouse. Covers severity ordering, repo-specific checks, and the closing checklist for approving a change.
---

# Review skill

For substantive review, read `.agents/architecture.md` first so the invariants below have context. For test-running and validation commands, use the `testing` skill rather than reasoning from memory.

## Severity order

Order findings by impact, highest first:

1. Correctness bugs.
2. Safety regressions: read-only enforcement, destructive-op gating, HTTP/SSE authentication.
3. Regressions in observable behavior: tool names, tool argument shapes, tool return shapes, prompt names, environment variable semantics, `/health` contract.
4. Public API and compatibility risk.
5. Backend-enablement regressions across the ClickHouse and chDB combinations.
6. Packaging and optional-dependency regressions, especially around the `chdb` extra.
7. Resource and timeout behavior: thread pool, query timeout, pagination cache.
8. Missing or weak tests.
9. Style and nits.

## Repo-specific checklist

When reviewing, explicitly check whether the change affects:

- tool names, argument names, argument defaults, or return shapes.
- prompt names or prompt registration.
- environment variable names, defaults, or semantics. If yes, `README.md` should change too.
- read-only enforcement in `get_readonly_setting` and `build_query_settings`.
- destructive-op detection in `_validate_query_for_destructive_ops`. Watch the regex for breadth, case handling, newline handling, and new DDL forms.
- HTTP and SSE authentication wiring and the token-or-disabled invariant.
- `/health` response contract across the four backend-enablement combinations.
- behavior when the `chdb` extra is missing. The server should warn and skip registration, not crash.
- config singletons. Flag any code path that mutates env vars without resetting the singleton.
- pagination token lifecycle: creation, reuse across filter changes, TTL eviction.
- query thread pool behavior and the `CLICKHOUSE_MCP_QUERY_TIMEOUT` contract.
- context-based client config overrides in `create_clickhouse_client` and the non-dict warning path.
- middleware loader behavior, including error handling and the `setup_middleware` contract.
- compatibility expectations for Python, ClickHouse, `fastmcp`, and `clickhouse-connect` versions covered in CI.

For tool-surface changes, confirm tests exercise the behavior through `fastmcp.Client`, not only the underlying function.

## Verifying claims

When a finding or assumption depends on library or server behavior that may have changed, use the `upstream-verify` skill to check it against the actual upstream source. Note the version or commit you checked. Flag any claim in the diff that looks version-sensitive and was not verified.

## Output structure

Use this shape:

1. Findings, ordered by severity.
2. Open questions or assumptions.
3. Brief change summary, only if useful.

Each finding should answer:

- what is wrong
- why it matters
- who or what it could break (Claude Desktop configs, existing tool arguments, existing env vars, etc.)
- what evidence in the diff or repo context supports the concern

Lead with findings, not summary. Use `file:line` references. Be explicit about impact. Distinguish confirmed issues from inferred risk.

If no material issues are found, say so explicitly and mention any residual gaps: chDB path not exercised, HTTP transport not smoke-tested, safety matrix not re-validated, etc.

## Closing checklist

Before saying a change looks good, confirm you understand:

- whether the MCP tool surface (names, arguments, returns) changed, intentionally or not.
- whether any environment variable behavior changed and whether `README.md` reflects it.
- whether safety defaults still hold: read-only by default, DROP gated behind `CLICKHOUSE_ALLOW_DROP`, HTTP/SSE auth required unless explicitly disabled.
- whether the change holds up across the ClickHouse-on, ClickHouse-off, chDB-on, chDB-off matrix.
- whether the `chdb` extra still being optional is respected.
- whether transport behavior still works for `stdio`, `http`, and `sse`.
- whether tests target the right layer: unit config tests with `monkeypatch`, tool tests through a live ClickHouse, MCP-level tests via `fastmcp.Client`.
- whether any important validation still has not been run, for example a local server round trip or a container-image smoke test.
