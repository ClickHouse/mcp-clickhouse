import pytest

from mcp_clickhouse import mcp_server


@pytest.fixture(autouse=True)
def clear_health_result_cache():
    """Keep the short-lived health result cache from leaking between tests."""
    mcp_server._health._clear_health_result_cache()
    yield
    mcp_server._health._clear_health_result_cache()
