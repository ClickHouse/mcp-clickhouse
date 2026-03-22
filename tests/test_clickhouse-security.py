import pytest
import re
from unittest.mock import MagicMock, patch
from fastmcp.exceptions import ToolError

# Import the actual unpatched validation function
from mcp_clickhouse.mcp_server import _validate_query_for_destructive_ops

def test_destructive_query_bypasses_filter_in_default_mode():
    """
    Proves that DROP TABLE bypasses the security filter in default read-only mode.
    The unpatched server skips application-layer validation and blindly trusts 
    the driver's readonly=1 flag, which can be overridden by DB user profiles.
    """
    with patch('mcp_clickhouse.mcp_server.get_config') as mock_get_config:
        mock_config = MagicMock()
        mock_config.allow_write_access = False # Simulate default mode
        mock_get_config.return_value = mock_config

        # Without our patch, this will FAIL because it doesn't raise a ToolError.
        with pytest.raises(ToolError):
            _validate_query_for_destructive_ops("DROP TABLE production_users;")

def test_sql_injection_comment_bypass_when_writes_enabled():
    """
    Proves that even when writes are enabled, the regex filter is weak and 
    can be bypassed using SQL comments to hide a DROP statement.
    """
    with patch('mcp_clickhouse.mcp_server.get_config') as mock_get_config:
        mock_config = MagicMock()
        mock_config.allow_write_access = True 
        mock_config.allow_drop = False # Drops should be blocked
        mock_get_config.return_value = mock_config

        malicious_query = "/* legitimate comment */ DROP TABLE users;"

        # Without our Regex Firewall patch, this will FAIL because it doesn't catch the bypass.
        with pytest.raises(ToolError):
             _validate_query_for_destructive_ops(malicious_query)
