"""
MCP ClickHouse - Model Context Protocol server for ClickHouse database integration.

This module provides the entry point for running the MCP ClickHouse server,
which enables AI models to interact with ClickHouse databases through a set of
well-defined tools.
"""

from .mcp_server import mcp


def main():
    """Run the MCP ClickHouse server."""
    mcp.run()


if __name__ == "__main__":
    main()
