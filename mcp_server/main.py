"""MCP server main entry point."""

import logging
import os

from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .config import get_myscale_config, get_chdb_config, get_mcp_config, TransportType

# Configure logging
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mcp-myscaledb")

# Load environment variables
load_dotenv()

# Create MCP instance
MCP_SERVER_NAME = "mcp-myscaledb"
mcp = FastMCP(name=MCP_SERVER_NAME)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    """Health check endpoint for monitoring server status.

    Returns OK if the server is running and can connect to MyScaleDB.
    """
    try:
        # Check if MyScaleDB is enabled by trying to create config
        myscale_enabled = os.getenv("MYSCALE_ENABLED", "true").lower() == "true"

        if not myscale_enabled:
            # If MyScaleDB is disabled, check chDB status
            chdb_config = get_chdb_config()
            if chdb_config.enabled:
                return PlainTextResponse("OK - MCP server running with chDB enabled")
            else:
                # Both MyScaleDB and chDB are disabled - this is an error
                return PlainTextResponse(
                    "ERROR - Both MyScaleDB and chDB are disabled. At least one must be enabled.",
                    status_code=503,
                )

        # Try to create a client connection to verify MyScaleDB connectivity
        from .myscaledb import create_myscale_client
        client = create_myscale_client()
        version = client.server_version
        return PlainTextResponse(f"OK - Connected to MyScaleDB {version}")
    except Exception as e:
        # Return 503 Service Unavailable if we can't connect to MyScaleDB
        return PlainTextResponse(f"ERROR - Cannot connect to MyScaleDB: {str(e)}", status_code=503)


def register_services():
    """Register all services based on configuration."""
    # Register MyScaleDB service
    if os.getenv("MYSCALE_ENABLED", "true").lower() == "true":
        from .myscaledb import register_tools as register_myscale_tools
        register_myscale_tools(mcp)
        logger.info("MyScaleDB service registered")

    # Register chDB service
    if os.getenv("CHDB_ENABLED", "false").lower() == "true":
        from .chdb import register_tools as register_chdb_tools
        register_chdb_tools(mcp)
        logger.info("chDB service registered")

    # Register pgvector service
    if os.getenv("PGVECTOR_ENABLED", "false").lower() == "true":
        from .pgvector import register_tools as register_pgvector_tools
        register_pgvector_tools(mcp)
        logger.info("pgvector service registered")


def main():
    """Start the MCP server."""
    # Register all services
    register_services()
    
    # Get server configuration
    mcp_config = get_mcp_config()
    transport = mcp_config.server_transport

    # For HTTP and SSE transports, we need to specify host and port
    http_transports = [TransportType.HTTP.value, TransportType.SSE.value]
    if transport in http_transports:
        # Use the configured bind host (defaults to 127.0.0.1, can be set to 0.0.0.0)
        # and bind port (defaults to 8000)
        logger.info(f"Starting MCP server, transport={transport}, host={mcp_config.bind_host}, port={mcp_config.bind_port}")
        mcp.run(transport=transport, host=mcp_config.bind_host, port=mcp_config.bind_port)
    else:
        # For stdio transport, no host or port is needed
        logger.info(f"Starting MCP server, transport={transport}")
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
