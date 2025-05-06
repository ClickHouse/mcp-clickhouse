import click
from .mcp_server import mcp


@click.command()
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse"]),
    default="stdio",
    help="Transport type",
)
@click.option("--port", default=8000, help="Port to listen on for SSE")
def main(port: int, transport: str):
    if transport == "sse":
        mcp.port = port
        mcp.run(transport=transport)
    elif transport == "stdio":
        mcp.run()


if __name__ == "__main__":
    print("Starting MCP ClickHouse server")
    main()
