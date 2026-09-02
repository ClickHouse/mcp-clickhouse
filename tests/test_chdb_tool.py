import concurrent.futures
import importlib.util
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from dotenv import load_dotenv
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from mcp_clickhouse import mcp_server
from mcp_clickhouse.chdb_prompt import CHDB_PROMPT

load_dotenv()

requires_chdb = pytest.mark.skipif(
    importlib.util.find_spec("chdb") is None, reason="requires chdb extra"
)


@unittest.skipUnless(importlib.util.find_spec("chdb") is not None, "requires chdb extra")
class TestChDBTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Set up the environment before chDB tests."""
        cls._previous_chdb_enabled = os.environ.get("CHDB_ENABLED")
        cls._previous_chdb_client = mcp_server._chdb_client
        cls._previous_chdb_error_message = mcp_server._chdb_error_message

        os.environ["CHDB_ENABLED"] = "true"
        if mcp_server._chdb_client is None:
            mcp_server._chdb_client = mcp_server._init_chdb_client()
        cls.client = mcp_server.create_chdb_client()
        cls._created_client = cls._previous_chdb_client is None and cls.client is mcp_server._chdb_client

    @classmethod
    def tearDownClass(cls):
        """Restore module and environment state after chDB tests."""
        if getattr(cls, "_created_client", False):
            cls.client.close()

        mcp_server._chdb_client = cls._previous_chdb_client
        mcp_server._chdb_error_message = cls._previous_chdb_error_message

        if cls._previous_chdb_enabled is None:
            os.environ.pop("CHDB_ENABLED", None)
        else:
            os.environ["CHDB_ENABLED"] = cls._previous_chdb_enabled

    def test_run_chdb_select_query_simple(self):
        """Test running a simple SELECT query in chDB."""
        query = "SELECT 1 as test_value"
        result = json.loads(mcp_server.run_chdb_select_query(query))
        self.assertIsInstance(result, list)
        self.assertIn("test_value", str(result))

    def test_run_chdb_select_query_with_file_table_function(self):
        """Test running a SELECT query against a local file via chDB."""
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as temp_file:
            temp_file.write("value\n1\n2\n3\n")
            temp_path = temp_file.name

        self.addCleanup(lambda: os.path.exists(temp_path) and os.unlink(temp_path))
        query = f"SELECT SUM(value) AS total FROM file('{temp_path}', 'CSVWithNames')"
        result = json.loads(mcp_server.run_chdb_select_query(query))
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["total"], 6)

    def test_run_chdb_select_query_failure(self):
        """A failing chDB query raises ToolError from the sync helper, like run_query."""
        query = "SELECT * FROM non_existent_table_chDB"
        with self.assertRaises(ToolError) as raised:
            mcp_server.run_chdb_select_query(query)
        self.assertIn("chDB query failed: ", str(raised.exception))
        self.assertIn("non_existent_table_chDB", str(raised.exception))

    def test_run_chdb_select_query_empty_result(self):
        """Test running a SELECT query that returns empty result in chDB."""
        query = "SELECT 1 WHERE 1 = 0"
        result = json.loads(mcp_server.run_chdb_select_query(query))
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)

    def test_run_chdb_select_query_wide_nested_integers(self):
        query = """
            SELECT
                CAST('340282366920938463463374607431768211455', 'UInt128') AS wide,
                [
                    CAST('170141183460469231731687303715884105727', 'Int128'),
                    CAST(7, 'Int128')
                ] AS nested
        """

        result = json.loads(mcp_server.run_chdb_select_query(query))

        self.assertEqual(
            result,
            [
                {
                    "wide": "340282366920938463463374607431768211455",
                    "nested": ["170141183460469231731687303715884105727", 7],
                }
            ],
        )


@pytest.fixture
def chdb_mcp(monkeypatch):
    """A fresh FastMCP instance with the chDB tool and prompt registered.

    Runs the real ``_register_chdb_tools`` against a throwaway server so the
    registered names, descriptions, and schemas are the production ones, without
    touching the module singleton. Reuses an existing chDB client when the module
    already created one (CHDB_ENABLED=true at import) and otherwise closes the one
    it creates.
    """
    monkeypatch.setenv("CHDB_ENABLED", "true")
    server = FastMCP("chdb-boundary-test")
    existing_client = mcp_server._chdb_client
    monkeypatch.setattr(mcp_server, "mcp", server)
    monkeypatch.setattr(mcp_server, "_chdb_error_message", None)
    monkeypatch.setattr(mcp_server.atexit, "register", lambda *args, **kwargs: None)
    if existing_client is not None:
        monkeypatch.setattr(mcp_server, "_init_chdb_client", lambda: existing_client)
    monkeypatch.setattr(mcp_server, "_chdb_client", None)

    mcp_server._register_chdb_tools()
    created_client = mcp_server._chdb_client
    assert created_client is not None, mcp_server._chdb_error_message
    try:
        yield server
    finally:
        if created_client is not existing_client:
            created_client.close()


@requires_chdb
@pytest.mark.asyncio
async def test_chdb_tool_is_exposed_over_mcp(chdb_mcp):
    async with Client(chdb_mcp) as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools] == ["run_chdb_select_query"]
    tool = tools[0]
    assert tool.description.startswith("Run SQL in chDB, an in-process ClickHouse engine.")
    assert "decimal strings" in tool.description
    assert tool.input_schema["required"] == ["query"]
    query_schema = tool.input_schema["properties"]["query"]
    assert query_schema["type"] == "string"
    assert query_schema["description"].startswith("The SQL statement to run in chDB.")


@requires_chdb
@pytest.mark.asyncio
async def test_chdb_select_returns_json_encoded_rows_over_mcp(chdb_mcp):
    async with Client(chdb_mcp) as client:
        result = await client.call_tool(
            "run_chdb_select_query", {"query": "SELECT 1 AS test_value, 'a' AS label"}
        )

    assert result.is_error is False
    assert len(result.content) == 1
    assert isinstance(result.content[0].text, str)
    assert json.loads(result.content[0].text) == [{"test_value": 1, "label": "a"}]


@requires_chdb
@pytest.mark.asyncio
async def test_chdb_query_failure_is_a_tool_error(chdb_mcp):
    """A failing chDB query is an MCP tool error (isError), like run_query.

    Until this release the tool answered with a successful result carrying
    ``{"status": "error", "message": ...}``; D30 aligned it with run_query.
    """
    query = "SELECT * FROM non_existent_table_chDB"

    async with Client(chdb_mcp) as client:
        result = await client.call_tool(
            "run_chdb_select_query", {"query": query}, raise_on_error=False
        )

    assert result.is_error is True
    text = result.content[0].text
    assert text.startswith("chDB query failed: ")
    assert "non_existent_table_chDB" in text
    assert "Traceback" not in text
    assert '"status"' not in text


@requires_chdb
@pytest.mark.asyncio
async def test_chdb_unexpected_exception_is_a_tool_error(chdb_mcp):
    def explode(_query):
        raise RuntimeError("engine crashed at /var/lib/chdb/private")

    with patch.object(mcp_server, "execute_chdb_query", side_effect=explode):
        async with Client(chdb_mcp) as client:
            result = await client.call_tool(
                "run_chdb_select_query", {"query": "SELECT 1"}, raise_on_error=False
            )

    assert result.is_error is True
    assert result.content[0].text == "chDB query failed: engine crashed at /var/lib/chdb/private"


@requires_chdb
@pytest.mark.asyncio
async def test_chdb_timeout_is_a_tool_error(chdb_mcp):
    """A pending chDB future past CLICKHOUSE_MCP_QUERY_TIMEOUT is a tool error."""
    pending = concurrent.futures.Future()

    with (
        patch.object(mcp_server, "QUERY_EXECUTOR") as query_executor,
        patch.object(
            mcp_server, "get_mcp_config", return_value=SimpleNamespace(query_timeout=0.02)
        ),
    ):
        query_executor.submit.return_value = pending
        async with Client(chdb_mcp) as client:
            result = await client.call_tool(
                "run_chdb_select_query", {"query": "SELECT sleep(3)"}, raise_on_error=False
            )

    assert result.is_error is True
    assert result.content[0].text == "chDB query timed out after 0.02 seconds"
    assert pending.cancelled()


@requires_chdb
@pytest.mark.asyncio
async def test_chdb_prompt_is_exposed_over_mcp(chdb_mcp):
    async with Client(chdb_mcp) as client:
        prompts = await client.list_prompts()
        prompt_result = await client.get_prompt("chdb_initial_prompt")

    assert [prompt.name for prompt in prompts] == ["chdb_initial_prompt"]
    assert prompts[0].description == (
        "This prompt helps users understand how to interact and perform common "
        "operations in chDB"
    )
    assert prompts[0].arguments in (None, [])
    assert len(prompt_result.messages) == 1
    message = prompt_result.messages[0]
    assert message.role == "user"
    assert message.content.text == CHDB_PROMPT


if __name__ == "__main__":
    unittest.main()
