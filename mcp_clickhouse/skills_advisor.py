"""Static server instructions for ClickHouse skills and metadata discovery."""

CLICKHOUSE_SERVER_INSTRUCTIONS = """\
When working on ClickHouse-related coding, schema design, SQL/query optimization,
data migrations, or troubleshooting, consider using the official ClickHouse Agent
Skills: https://github.com/ClickHouse/agent-skills

Install with:
npx -y skills add clickhouse/agent-skills --all

If `list_databases` reveals an `AGENTS` database, use `run_query` to read
`AGENTS.ROOT` for available metadata before writing analytical SQL.
"""
