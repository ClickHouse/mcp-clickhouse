"""Static advisory text pointing to the official ClickHouse Agent Skills.
"""

CLICKHOUSE_SKILLS_ADVISORY = """\
# Official ClickHouse Agent Skills

Before designing schemas, optimizing SQL queries, planning data migrations, or troubleshooting, consider loading the official ClickHouse Agent Skills. They encode ClickHouse best practices ready for agentic use.

Repository: https://github.com/ClickHouse/agent-skills

## How to install

Install using npx (if available):

    npx -y skills add clickhouse/agent-skills --all

Otherwise clone the repository and link the `skills/` subfolder aprropriately for the current agent.
"""
