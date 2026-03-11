from unittest.mock import MagicMock, patch

import pytest

from mcp_clickhouse import mcp_server


def test_create_chdb_client_surfaces_optional_dependency_message():
    with (
        patch.dict("os.environ", {"CHDB_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_chdb_client", None),
        patch.object(
            mcp_server,
            "_chdb_error_message",
            "chDB support requires the optional dependency. "
            "Install mcp-clickhouse[chdb] to enable chDB features.",
        ),
    ):
        with pytest.raises(RuntimeError, match=r"mcp-clickhouse\[chdb\]"):
            mcp_server.create_chdb_client()


def test_register_chdb_tools_skips_when_client_is_unavailable():
    with (
        patch.dict("os.environ", {"CHDB_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_init_chdb_client", return_value=None),
        patch.object(mcp_server, "_chdb_client", None),
        patch.object(mcp_server.mcp, "add_tool") as add_tool,
        patch.object(mcp_server.mcp, "add_prompt") as add_prompt,
    ):
        mcp_server._register_chdb_tools()

    add_tool.assert_not_called()
    add_prompt.assert_not_called()


def test_register_chdb_tools_registers_when_client_is_available():
    mock_client = MagicMock()
    with (
        patch.dict("os.environ", {"CHDB_ENABLED": "true"}, clear=False),
        patch.object(mcp_server, "_init_chdb_client", return_value=mock_client),
        patch.object(mcp_server, "_chdb_client", None),
        patch.object(mcp_server.mcp, "add_tool") as add_tool,
        patch.object(mcp_server.mcp, "add_prompt") as add_prompt,
    ):
        mcp_server._register_chdb_tools()

    add_tool.assert_called_once()
    add_prompt.assert_called_once()
