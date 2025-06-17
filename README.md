# ClickHouse MCP Server

[![PyPI - Version](https://img.shields.io/pypi/v/mcp-clickhouse)](https://pypi.org/project/mcp-clickhouse)

An MCP server for ClickHouse.

<a href="https://glama.ai/mcp/servers/yvjy4csvo1"><img width="380" height="200" src="https://glama.ai/mcp/servers/yvjy4csvo1/badge" alt="mcp-clickhouse MCP server" /></a>

## Features

### ClickHouse Tools

* `run_select_query`
  * Execute SQL queries on your ClickHouse cluster.
  * Input: `sql` (string): The SQL query to execute.
  * All ClickHouse queries are run with `readonly = 1` to ensure they are safe.

* `list_databases`
  * List all databases on your ClickHouse cluster.

* `list_tables`
  * List all tables in a database.
  * Input: `database` (string): The name of the database.

### chDB Tools

* `run_chdb_select_query`
  * Execute SQL queries using chDB's embedded OLAP engine.
  * Input: `sql` (string): The SQL query to execute.
  * Query data directly from various sources (files, URLs, databases) without ETL processes.

## Configuration

This MCP server supports both ClickHouse and chDB. You can enable either or both depending on your needs.

1. Open the Claude Desktop configuration file located at:
   * On macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   * On Windows: `%APPDATA%/Claude/claude_desktop_config.json`

2. Add the following:

```json
{
  "mcpServers": {
    "mcp-clickhouse": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "mcp-clickhouse",
        "--python",
        "3.13",
        "mcp-clickhouse"
      ],
      "env": {
        "CLICKHOUSE_HOST": "<clickhouse-host>",
        "CLICKHOUSE_PORT": "<clickhouse-port>",
        "CLICKHOUSE_USER": "<clickhouse-user>",
        "CLICKHOUSE_PASSWORD": "<clickhouse-password>",
        "CLICKHOUSE_SECURE": "true",
        "CLICKHOUSE_VERIFY": "true",
        "CLICKHOUSE_CONNECT_TIMEOUT": "30",
        "CLICKHOUSE_SEND_RECEIVE_TIMEOUT": "30"
      }
    }
  }
}
```

Update the environment variables to point to your own ClickHouse service.

Or, if you'd like to try it out with the [ClickHouse SQL Playground](https://sql.clickhouse.com/), you can use the following config:

```json
{
  "mcpServers": {
    "mcp-clickhouse": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "mcp-clickhouse",
        "--python",
        "3.13",
        "mcp-clickhouse"
      ],
      "env": {
        "CLICKHOUSE_HOST": "sql-clickhouse.clickhouse.com",
        "CLICKHOUSE_PORT": "8443",
        "CLICKHOUSE_USER": "demo",
        "CLICKHOUSE_PASSWORD": "",
        "CLICKHOUSE_SECURE": "true",
        "CLICKHOUSE_VERIFY": "true",
        "CLICKHOUSE_CONNECT_TIMEOUT": "30",
        "CLICKHOUSE_SEND_RECEIVE_TIMEOUT": "30"
      }
    }
  }
}
```

For chDB (embedded OLAP engine), add the following configuration:

```json
{
  "mcpServers": {
    "mcp-clickhouse": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "mcp-clickhouse",
        "--python",
        "3.13",
        "mcp-clickhouse"
      ],
      "env": {
        "CHDB_ENABLED": "true",
        "CLICKHOUSE_ENABLED": "false",
        "CHDB_DATA_PATH": "/path/to/chdb/data"
      }
    }
  }
}
```

You can also enable both ClickHouse and chDB simultaneously:

```json
{
  "mcpServers": {
    "mcp-clickhouse": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "mcp-clickhouse",
        "--python",
        "3.13",
        "mcp-clickhouse"
      ],
      "env": {
        "CLICKHOUSE_HOST": "<clickhouse-host>",
        "CLICKHOUSE_PORT": "<clickhouse-port>",
        "CLICKHOUSE_USER": "<clickhouse-user>",
        "CLICKHOUSE_PASSWORD": "<clickhouse-password>",
        "CLICKHOUSE_SECURE": "true",
        "CLICKHOUSE_VERIFY": "true",
        "CLICKHOUSE_CONNECT_TIMEOUT": "30",
        "CLICKHOUSE_SEND_RECEIVE_TIMEOUT": "30",
        "CHDB_ENABLED": "true",
        "CHDB_DATA_PATH": "/path/to/chdb/data"
      }
    }
  }
}
```

3. Locate the command entry for `uv` and replace it with the absolute path to the `uv` executable. This ensures that the correct version of `uv` is used when starting the server. On a mac, you can find this path using `which uv`.

4. Restart Claude Desktop to apply the changes.

## Development

1. In `test-services` directory run `docker compose up -d` to start the ClickHouse cluster.

2. Add the following variables to a `.env` file in the root of the repository.

*Note: The use of the `default` user in this context is intended solely for local development purposes.*

```bash
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=clickhouse
```

3. Run `uv sync` to install the dependencies. To install `uv` follow the instructions [here](https://docs.astral.sh/uv/). Then do `source .venv/bin/activate`.

4. For easy testing with the MCP Inspector, run `fastmcp dev mcp_clickhouse/mcp_server.py` to start the MCP server.

### Environment Variables

The following environment variables are used to configure the ClickHouse and chDB connections:

#### ClickHouse Variables

##### Required Variables

* `CLICKHOUSE_HOST`: The hostname of your ClickHouse server
* `CLICKHOUSE_USER`: The username for authentication
* `CLICKHOUSE_PASSWORD`: The password for authentication

> [!CAUTION]
> It is important to treat your MCP database user as you would any external client connecting to your database, granting only the minimum necessary privileges required for its operation. The use of default or administrative users should be strictly avoided at all times.

##### Optional Variables

* `CLICKHOUSE_PORT`: The port number of your ClickHouse server
  * Default: `8443` if HTTPS is enabled, `8123` if disabled
  * Usually doesn't need to be set unless using a non-standard port
* `CLICKHOUSE_SECURE`: Enable/disable HTTPS connection
  * Default: `"true"`
  * Set to `"false"` for non-secure connections
* `CLICKHOUSE_VERIFY`: Enable/disable SSL certificate verification
  * Default: `"true"`
  * Set to `"false"` to disable certificate verification (not recommended for production)
* `CLICKHOUSE_CONNECT_TIMEOUT`: Connection timeout in seconds
  * Default: `"30"`
  * Increase this value if you experience connection timeouts
* `CLICKHOUSE_SEND_RECEIVE_TIMEOUT`: Send/receive timeout in seconds
  * Default: `"300"`
  * Increase this value for long-running queries
* `CLICKHOUSE_DATABASE`: Default database to use
  * Default: None (uses server default)
  * Set this to automatically connect to a specific database
* `CLICKHOUSE_MCP_SERVER_TRANSPORT`: Sets the transport method for the MCP server.
  * Default: `"stdio"`
  * Valid options: `"stdio"`, `"http"`, `"streamable-http"`, `"sse"`. This is useful for local development with tools like MCP Inspector.
* `CLICKHOUSE_ENABLED`: Enable/disable ClickHouse functionality
  * Default: `"true"`
  * Set to `"false"` to disable ClickHouse tools when using chDB only

#### chDB Variables

* `CHDB_ENABLED`: Enable/disable chDB functionality
  * Default: `"false"`
  * Set to `"true"` to enable chDB tools
* `CHDB_DATA_PATH`: The path to the chDB data directory
  * Default: `":memory:"` (in-memory database) 
  * Use `:memory:` for in-memory database
  * Use a file path for persistent storage (e.g., `/path/to/chdb/data`)

#### Example Configurations

For local development with Docker:

```env
# Required variables
CLICKHOUSE_HOST=localhost
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=clickhouse

# Optional: Override defaults for local development
CLICKHOUSE_SECURE=false  # Uses port 8123 automatically
CLICKHOUSE_VERIFY=false
```

For ClickHouse Cloud:

```env
# Required variables
CLICKHOUSE_HOST=your-instance.clickhouse.cloud
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=your-password

# Optional: These use secure defaults
# CLICKHOUSE_SECURE=true  # Uses port 8443 automatically
# CLICKHOUSE_DATABASE=your_database
```

For ClickHouse SQL Playground:

```env
CLICKHOUSE_HOST=sql-clickhouse.clickhouse.com
CLICKHOUSE_USER=demo
CLICKHOUSE_PASSWORD=
# Uses secure defaults (HTTPS on port 8443)
```

For chDB only (in-memory):

```env
# chDB configuration
CHDB_ENABLED=true
CLICKHOUSE_ENABLED=false
# CHDB_DATA_PATH defaults to :memory:
```

For chDB with persistent storage:

```env
# chDB configuration
CHDB_ENABLED=true
CLICKHOUSE_ENABLED=false
CHDB_DATA_PATH=/path/to/chdb/data
```

You can set these variables in your environment, in a `.env` file, or in the Claude Desktop configuration:

```json
{
  "mcpServers": {
    "mcp-clickhouse": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "mcp-clickhouse",
        "--python",
        "3.13",
        "mcp-clickhouse"
      ],
      "env": {
        "CLICKHOUSE_HOST": "<clickhouse-host>",
        "CLICKHOUSE_USER": "<clickhouse-user>",
        "CLICKHOUSE_PASSWORD": "<clickhouse-password>",
        "CLICKHOUSE_DATABASE": "<optional-database>"
      }
    }
  }
}
```

### Running tests

```bash
uv sync --all-extras --dev # install dev dependencies
uv run ruff check . # run linting

docker compose up -d test_services # start ClickHouse
uv run pytest -v tests
uv run pytest -v tests/test_tool.py # ClickHouse only
uv run pytest -v tests/test_chdb_tool.py # chDB only
```

## YouTube Overview

[![YouTube](http://i.ytimg.com/vi/y9biAm_Fkqw/hqdefault.jpg)](https://www.youtube.com/watch?v=y9biAm_Fkqw)
