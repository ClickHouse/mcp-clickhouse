from .mcp_server import mcp
from mcp_clickhouse.mcp_env import get_config


def main():
    transport = get_config().transport
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
