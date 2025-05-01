import asyncio
import argparse

from .mcp_server import mcp


def main():
    mcp.run()


def main_sse():
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
