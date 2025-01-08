# ClickHouse MCP Server

An MCP server for ClickHouse.

<a href="https://glama.ai/mcp/servers/yvjy4csvo1"><img width="380" height="200" src="https://glama.ai/mcp/servers/yvjy4csvo1/badge" alt="mcp-clickhouse MCP server" /></a>

## Features

### Tools

* `run_select_query`
  - Execute SQL queries on your ClickHouse cluster.
  - Input: `sql` (string): The SQL query to execute.
  - All ClickHouse queries are run with `readonly = 1` to ensure they are safe.

* `list_databases`
  - List all databases on your ClickHouse cluster.

* `list_tables`
  - List all tables in a database.
  - Input: `database` (string): The name of the database.

## Configuration

> **Note**: This is a temporary configuration process that will be significantly improved once the package is published.

1. Run `uv sync` to install the dependencies. To install `uv` follow the instructions [here](https://docs.astral.sh/uv/). Then do `source .venv/bin/activate`.

2. Setup the `.env.production` file with the ClickHouse credentials.

```
CLICKHOUSE_HOST=<CLICKHOUSE_HOST>
CLICKHOUSE_PORT=<CLICKHOUSE_PORT>
CLICKHOUSE_USER=<CLICKHOUSE_USER>
CLICKHOUSE_PASSWORD=<CLICKHOUSE_PASSWORD>
```

3. Run `fastmcp install mcp_clickhouse/mcp_server.py -f .env.production` to install the server.

4. Open the configuration file located at:
   - On macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - On Windows: `%APPDATA%/Claude/claude_desktop_config.json`

   Locate the command entry for `uv` and replace it with the absolute path to the `uv` executable. This ensures that the correct version of `uv` is used when starting the server.

5. Restart Claude Desktop to apply the changes.


## Development

1. In `test-services` directory run `docker compose up -d` to start the ClickHouse cluster.

2. Add the following variables to a `.env` file in the root of the repository.

```
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=clickhouse
```

3. Run `uv sync` to install the dependencies. To install `uv` follow the instructions [here](https://docs.astral.sh/uv/). Then do `source .venv/bin/activate`.

4. For easy testing, you can run `fastmcp dev mcp_clickhouse/mcp_server.py` to start the MCP server.
