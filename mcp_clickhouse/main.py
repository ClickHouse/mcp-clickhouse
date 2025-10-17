from .mcp_server import mcp
from .mcp_env import get_mcp_config, TransportType


def main():
    mcp_config = get_mcp_config()
    transport = mcp_config.server_transport

    # For HTTP and SSE transports, we need to specify host and port
    http_transports = [TransportType.HTTP.value, TransportType.SSE.value]
    if transport in http_transports:
        # Use the configured bind host (defaults to 127.0.0.1, can be set to 0.0.0.0)
        # and bind port (defaults to 8000)
        mcp.run(transport=transport, host=mcp_config.bind_host, port=mcp_config.bind_port)
    else:
        # For stdio transport, no host or port is needed
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
