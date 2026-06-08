import unittest

from fastmcp.tools import Tool

from mcp_clickhouse import clickhouse_skills_advisor
from mcp_clickhouse.skills_advisor import CLICKHOUSE_SKILLS_ADVISORY


class TestSkillsAdvisorTool(unittest.TestCase):
    def test_returns_non_empty_description(self):
        result = clickhouse_skills_advisor()
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())
        self.assertEqual(result, CLICKHOUSE_SKILLS_ADVISORY)

    def test_mentions_skills_repo_and_install_hint(self):
        result = clickhouse_skills_advisor()
        self.assertIn("github.com/ClickHouse/agent-skills", result)
        self.assertIn("npx skills", result)

    def test_tool_can_be_registered(self):
        tool = Tool.from_function(
            clickhouse_skills_advisor,
            name="clickhouse_skills_advisor",
            description="MUST USE when starting to analyze a ClickHouse task",
        )
        self.assertEqual(tool.name, "clickhouse_skills_advisor")


if __name__ == "__main__":
    unittest.main()
