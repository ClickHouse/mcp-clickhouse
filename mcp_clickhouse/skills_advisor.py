"""Static advisory text pointing to the official ClickHouse Agent Skills.

This text is a manually maintained snapshot of the skills published at
https://github.com/ClickHouse/agent-skills. It is intentionally hardcoded so the
tool stays fast and works offline (no network fetch at call time).
"""

CLICKHOUSE_SKILLS_ADVISORY = """\
# Official ClickHouse Agent Skills

Before writing ClickHouse SQL, designing schemas, optimizing queries, planning
data migrations, or troubleshooting, consider loading the official ClickHouse
Agent Skills. They encode ClickHouse best practices so agents avoid common
pitfalls.

Repository: https://github.com/ClickHouse/agent-skills
Spec: https://agentskills.io

## What the skills cover

- **ClickHouse Best Practices** — schema design, primary key selection, data
  type selection, JOIN optimization, insert batching, mutation avoidance,
  partitioning, skipping indices, materialized views, async inserts, and JSON
  usage.
- **ClickHouse Architecture Advisor** — workload-aware decision frameworks for
  ingestion strategy, join & enrichment patterns, late-arriving data & upserts,
  time-series partitioning, and real-time pre-aggregation.
- **Troubleshooting** — diagnosing common failure modes, e.g. the ClickHouse
  Node.js client (`@clickhouse/client`): socket hang-up / `ECONNRESET`,
  Keep-Alive misconfiguration, data type mismatches, read-only user
  restrictions, TLS errors, and compression issues.
- **chDB / data migration** — Pandas-compatible API for chDB and patterns for
  querying and migrating data across 16+ sources (MySQL, PostgreSQL, S3,
  MongoDB, Iceberg, Delta Lake, etc.) without heavyweight ETL.

## How to install

Install the skills into your agent of choice (Claude Code, Cursor, Copilot,
etc.):

    npx skills add clickhouse/agent-skills

Or with the ClickHouse CLI:

    clickhousectl skills

Once installed, the relevant skill activates automatically when you work with
ClickHouse or chDB. This snapshot may lag the upstream repository; check the
repository above for the latest skills.
"""
