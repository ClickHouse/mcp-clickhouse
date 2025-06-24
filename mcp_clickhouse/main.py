from .mcp_server import mcp
from .mcp_env import get_config


def main():
    config = get_config()
    transport = config.mcp_server_transport
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
