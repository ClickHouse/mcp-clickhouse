---
name: upstream-verify
description: Use when a claim, fix, or review finding depends on ClickHouse, clickhouse-connect, FastMCP, or chDB behavior that could be version-sensitive. Use this rather than relying on training memory for library or server semantics.
---

# Upstream verification skill

Library and server behavior changes between versions. Memory of any of the projects below is likely stale. When a code change, bug claim, or review finding depends on how one of them actually behaves, verify against the upstream source rather than guessing.

Trigger this skill when:

- a query setting, readonly mode, error message, or type serialization claim depends on ClickHouse server behavior
- a connection, settings, or result-shape claim depends on `clickhouse-connect` driver semantics
- a tool registration, middleware hook, auth provider, or transport claim depends on `FastMCP`
- a chDB session, query, or in-process behavior claim depends on `chDB`
- the diff adjusts behavior tied to a specific upstream version and the diff does not say which version

## Where to look

| Concern | Source |
| --- | --- |
| ClickHouse server | https://github.com/ClickHouse/ClickHouse |
| `clickhouse-connect` (HTTP driver) | https://github.com/ClickHouse/clickhouse-connect |
| `FastMCP` (MCP server framework) | https://github.com/jlowin/fastmcp |
| `chDB` (in-process ClickHouse) | https://github.com/chdb-io/chdb |

Treat the source repo as authoritative over docs, blog posts, or memory. Docs lag.

## How to verify

Pick the lightest tool that answers the question:

- File-level inspection: `gh api repos/<org>/<repo>/contents/<path>?ref=<tag-or-sha>` returns base64 content.
- Symbol search: `gh api search/code -q "<symbol> repo:<org>/<repo>"`.
- Diff between versions: `gh api repos/<org>/<repo>/compare/<old>...<new>`.
- Changelog or release notes: `gh release view <tag> --repo <org>/<repo>` or read `CHANGELOG.md` from the right ref.
- For chDB and FastMCP, the README and tests in the repo are usually the fastest proof.

If the harness exposes web fetch or web search, those are valid too. The point is to ground the claim in something observable, not pin a specific tool.

## Pinning what you checked

When citing upstream behavior in a finding, code comment, or PR comment, note the version or commit you verified against. Future readers (and future agents) need that to re-check. A line like:

> Verified against `clickhouse-connect@0.8.16` (commit `<sha>`): `client.server_settings` returns `Setting` objects with a `.value` attribute.

is enough.

## When you cannot verify

If upstream is unreachable, pinned to a commit you cannot resolve, or the answer requires running the library, say so explicitly. Do not paper over it. Flag the unverified assumption in the diff or review and let a human resolve it.

## Compatibility floors that already matter

Keep these in mind when picking which version to verify against. The repo has to keep working across all of them.

- Python `3.10+` (declared floor in `pyproject.toml`). CI exercises Python `3.13`.
- ClickHouse server: CI runs against `clickhouse/clickhouse-server:24.10`. Behavior should stay reasonable across recent ClickHouse versions.
- `fastmcp` `>=2.0.0,<3.0.0`.
- `clickhouse-connect` `>=0.8.16`.
- Transports: `stdio`, `http`, `sse`. HTTP and SSE matter most when authentication or `/health` is in scope.
