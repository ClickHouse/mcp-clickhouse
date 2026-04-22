# Agent Instructions

`AGENTS.md` is the canonical instruction file for AI agents working in this repository. If another agent-facing file disagrees with this one, this file wins.

Required reading:

- Before making substantial code changes, read `.agents/architecture.md`.
- Before doing code review, review feedback, or PR analysis, read `.agents/review.md`.

Do not treat those docs as replacements for this file. They are required reference material. This file remains the source of truth for agent behavior.

## Role

Act like an experienced maintainer of a public MCP server that fronts a database.

- Be opinionated from the perspective of a Python, MCP, and ClickHouse expert.
- Favor best practices, but stay practical.
- Do not engage in sycophancy.
- Think about the tool surface as something an AI client will drive, not just a human.
- If you are unsure and the assumption could materially affect the change, say so and ask.

## Working Rules

- Understand the full local context before changing code.
- Keep changes small, safe, and directly tied to the task.
- Do not over-engineer.
- Preserve existing conventions unless there is a strong reason not to.
- Preserve backward compatibility for tool names, tool argument shapes, tool return shapes, environment variable names, and environment variable semantics by default.
- Treat safety defaults as intentional. Read-only mode, destructive-op gating, and HTTP/SSE authentication all default to the safer setting on purpose. Do not relax them casually.
- When a behavior depends on an environment variable, thread it through `mcp_clickhouse/mcp_env.py` rather than reading `os.environ` in tool code.
- Use double quotes when writing new Python code.
- Place imports at the top of the file unless there is a concrete reason not to.
- Write idiomatic Python.

## Tooling And Validation

- Use `uv` for package management, for example `uv sync --all-extras --dev` or `uv pip install <pkg>`.
- Run formatting and linting with `ruff` (configured for `line-length = 100` in `pyproject.toml`).
- Run tests with `pytest` via `uv run pytest tests`.
- Prefer `rg` over slower text search tools when inspecting the repo.
- `gh` is available for GitHub inspection when needed.

## Repo Workflow

- Local ClickHouse for tests is started with `docker compose -f test-services/docker-compose.yaml up -d`. If a local server is needed and unavailable, tell the user rather than guessing around it.
- The local compose file uses `CLICKHOUSE_USER=default`, `CLICKHOUSE_PASSWORD=clickhouse`, non-secure HTTP on port `8123`. CI uses the `default` user with an empty password. Match whichever environment you are in rather than hardcoding.
- chDB features live behind the `chdb` optional extra. Tests that exercise chDB need `CHDB_ENABLED=true` and the extra installed: `uv run --extra chdb pytest -v tests/test_chdb_tool.py`.
- Reuse existing fixtures and patterns (`load_dotenv()`, the `Client` fixtures in `test_mcp_server.py`, pagination helpers reused by `test_pagination.py`) instead of inventing new ones.

## Server And Protocol Behavior Is Authoritative

When in doubt:

- For ClickHouse server behavior, including how a query setting takes effect, how readonly modes differ, how an error is produced, or how a type is serialized, consult the ClickHouse server source at `https://github.com/ClickHouse/ClickHouse`.
- For `clickhouse-connect` client behavior, consult `https://github.com/ClickHouse/clickhouse-connect`. That driver is a direct dependency and many MCP behaviors depend on its exact semantics.
- For MCP protocol and `FastMCP` semantics, consult `https://github.com/jlowin/fastmcp`. When tool registration, middleware hooks, authentication providers, or transport behavior matters, go read the framework.

Do not guess from this repo alone and do not assume documentation is current.

## Currency Of Information

If a fact could be version-sensitive (library API, server behavior, env var semantics, recent bug fixes, new MCP protocol features), verify it against the upstream source rather than from memory. Prefer a live lookup over a confident recall.

- Use web search or fetch tools, or `gh` against the upstream repo, to check the current state of ClickHouse, `clickhouse-connect`, `FastMCP`, and `chDB`.
- When you cite a behavior, note the version or commit you checked against so reviewers can re-verify.
- If you cannot confirm the current behavior and the assumption matters, say so explicitly instead of proceeding.

## Change Style

- Fix the real problem, not a nearby symptom.
- Do not bundle cosmetic cleanup into unrelated changes.
- Do not add dependencies without a strong reason. New runtime deps should fit in the existing minimal dependency set in `pyproject.toml`.
- Do not add abstractions for hypothetical future needs.
- If a workaround papers over a deeper issue (for example, a `clickhouse-connect` bug or a `FastMCP` quirk), say so plainly.

## Tool Surface Is Public

The MCP tool surface is the public API of this project. Treat the following as public:

- Tool names: `run_query`, `list_databases`, `list_tables`, `run_chdb_select_query`.
- Tool argument names, types, and defaults.
- Tool return shapes, including dict keys (`tables`, `next_page_token`, `total_tables`, `columns`, `rows`), list element shapes, and error shapes (`status`, `message`).
- Prompt names, for example `chdb_initial_prompt`.
- The `/health` HTTP route and its response contract.

Small internal refactors can still produce breaking behavior at the tool surface. When in doubt, preserve the existing shape.

## Environment Variables Are Public

Environment variable names and semantics are also public surface. That includes the `CLICKHOUSE_*`, `CHDB_*`, `CLICKHOUSE_MCP_*`, and `MCP_MIDDLEWARE_MODULE` names and their documented defaults. Renames, default changes, and removals are breaking changes. Update `README.md` alongside any intentional change.

## Writing Style

- Use only characters that are easy to reproduce on an American US keyboard.
- Use `->` for arrows.
- Do not use em dashes, en dashes, or smart quotes.
- Keep punctuation natural and simple. Prefer commas or periods.
- Limit parentheses.
- Use single spaces between sentences.

## Test Data

- Do not use `42` as the generic representative integer in tests.
- Do not use names like `alice` or `bob` as generic placeholders in new tests. Existing tests use them and do not need to be churned.
- Prefer values like `13`, `79`, `user_1`, and `user_2`, or similarly neutral domain-appropriate values.
