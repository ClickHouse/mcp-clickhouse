import os
import asyncio
import uvicorn
from mcp_clickhouse.mcp_server import mcp, run_server

def get_mode():
    """Get the server mode from environment variable."""
    return os.getenv("MCP_SERVER_MODE", "http").lower()

def get_port():
    """Get the server port from environment variable."""
    return int(os.getenv("MCP_SERVER_PORT", "8213"))

def get_host():
    """Get the server host from environment variable."""
    return os.getenv("MCP_SERVER_HOST", "0.0.0.0")

async def main():
    """Main entry point that supports both HTTP and streaming modes."""
    mode = get_mode()
    
    if mode == "stream" or mode == "streaming":
        print("Starting MCP server in streaming mode...")
        await run_server()
    else:
        print(f"Starting MCP server in HTTP mode on {get_host()}:{get_port()}...")
        config = uvicorn.Config(
            mcp, 
            host=get_host(), 
            port=get_port(),
            log_level="info"
        )
        server = uvicorn.Server(config)
        await server.serve()

if __name__ == "__main__":
    asyncio.run(main())