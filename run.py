#!/usr/bin/env python3
"""
Launch script for MCP ClickHouse Server.

This script loads environment variables and starts the MCP server.
"""

import os
from dotenv import load_dotenv
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mcp-clickhouse")

def main():
    """Initialize environment and start the server."""
    logger.info("Initializing MCP ClickHouse server")
    
    # Load environment variables
    env_file = '.mcp_clickhouse_env'
    if os.path.exists(env_file):
        logger.info(f"Loading environment from {env_file}")
        load_dotenv(env_file)
    
    # Start MCP server
    logger.info("Starting MCP server")
    from mcp_clickhouse.main import main as mcp_main
    mcp_main()

if __name__ == "__main__":
    main() 