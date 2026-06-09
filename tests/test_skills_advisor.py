import unittest

from mcp_clickhouse.mcp_server import mcp
from mcp_clickhouse.skills_advisor import CLICKHOUSE_SERVER_INSTRUCTIONS


class TestSkillsAdvisorInstructions(unittest.TestCase):
    def test_server_instructions_are_configured(self):
        self.assertIsInstance(mcp.instructions, str)
        self.assertTrue(mcp.instructions.strip())
        self.assertEqual(mcp.instructions, CLICKHOUSE_SERVER_INSTRUCTIONS)

    def test_instructions_mention_skills_repo_and_install_hint(self):
        self.assertIn("github.com/ClickHouse/agent-skills", mcp.instructions)
        self.assertIn("skills add clickhouse/agent-skills", mcp.instructions)

    def test_skills_advisor_tool_not_registered(self):
        self.assertNotIn("clickhouse_skills_advisor", mcp._tool_manager._tools)


if __name__ == "__main__":
    unittest.main()
