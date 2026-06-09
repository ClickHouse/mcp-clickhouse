import asyncio
import unittest

from fastmcp import Client

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
        async def _list_tool_names():
            async with Client(mcp) as client:
                tools = await client.list_tools()

            names = set()
            for tool in tools:
                if isinstance(tool, str):
                    names.add(tool)
                elif isinstance(tool, dict):
                    names.add(tool.get("name"))
                else:
                    names.add(getattr(tool, "name", None))
            names.discard(None)
            return names

        tool_names = asyncio.run(_list_tool_names())
        self.assertNotIn("clickhouse_skills_advisor", tool_names)

if __name__ == "__main__":
    unittest.main()
