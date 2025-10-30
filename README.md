# MyScaleDB MCP Server

[![PyPI - Version](https://img.shields.io/pypi/v/mcp-clickhouse)](https://pypi.org/project/mcp-clickhouse)

An MCP server for MyScaleDB - combining analytical database power with vector search capabilities.

<a href="https://glama.ai/mcp/servers/yvjy4csvo1"><img width="380" height="200" src="https://glama.ai/mcp/servers/yvjy4csvo1/badge" alt="mcp-myscaledb MCP server" /></a>

## Features

### MyScaleDB Tools

* `run_select_query`
  * Execute standard SQL SELECT queries on your MyScaleDB cluster.
  * Input: `query` (string): The SQL SELECT query to execute.
  * Best for regular data analysis and aggregation queries.
  * All MyScaleDB queries are run with `readonly = 1` to ensure they are safe.

* `run_similarity_select_query`
  * Execute SELECT queries with vector search and full-text search capabilities.
  * Input: `query` (string): The SQL query with distance(), TextSearch(), or HybridSearch() functions.
  * Best for similarity search, semantic search, and hybrid search queries.
  * Supports: distance(), TextSearch(), HybridSearch() functions.

* `list_databases`
  * List all databases on your MyScaleDB cluster.

* `list_tables`
  * List all tables in a database.
  * Input: `database` (string): The name of the database.

### chDB Tools

* `run_chdb_select_query`
  * Execute SQL queries using [chDB](https://github.com/chdb-io/chdb)'s embedded ClickHouse engine.
  * Input: `sql` (string): The SQL query to execute.
  * Query data directly from various sources (files, URLs, databases) without ETL processes.

### pgvector Tools

* `run_pgvector_select_query`
  * Execute SELECT queries on PostgreSQL with pgvector extension
* `list_pgvector_tables`
  * List all tables in the PostgreSQL database
* `list_pgvector_vectors`
  * List all vector columns and their dimensions
* `search_similar_vectors`
  * Perform similarity search using vector embeddings

### Health Check Endpoint

When running with HTTP or SSE transport, a health check endpoint is available at `/health`. This endpoint:
- Returns `200 OK` with the MyScaleDB version if the server is healthy and can connect to MyScaleDB
- Returns `503 Service Unavailable` if the server cannot connect to MyScaleDB

Example:
```bash
curl http://localhost:8000/health
# Response: OK - Connected to MyScaleDB 24.3.1
```

## Configuration

This MCP server supports MyScaleDB, chDB, and pgvector. You can enable either or multiple services depending on your needs.

1. Open the Claude Desktop configuration file located at:
   * On macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   * On Windows: `%APPDATA%/Claude/claude_desktop_config.json`

2. Add the following:

```json
{
  "mcpServers": {
    "mcp-myscaledb": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "mcp-clickhouse",
        "--python",
        "3.10",
        "mcp-clickhouse"
      ],
      "env": {
        "MYSCALE_HOST": "<myscaledb-host>",
        "MYSCALE_PORT": "<myscaledb-port>",
        "MYSCALE_USER": "<myscaledb-user>",
        "MYSCALE_PASSWORD": "<myscaledb-password>",
        "MYSCALE_SECURE": "true",
        "MYSCALE_VERIFY": "true",
        "MYSCALE_CONNECT_TIMEOUT": "30",
        "MYSCALE_SEND_RECEIVE_TIMEOUT": "30"
      }
    }
  }
}
```

Update the environment variables to point to your own MyScaleDB service.

For chDB (embedded ClickHouse engine), add the following configuration:

```json
{
  "mcpServers": {
    "mcp-myscaledb": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "mcp-clickhouse",
        "--python",
        "3.10",
        "mcp-clickhouse"
      ],
      "env": {
        "CHDB_ENABLED": "true",
        "MYSCALE_ENABLED": "false",
        "CHDB_DATA_PATH": "/path/to/chdb/data"
      }
    }
  }
}
```

For pgvector (PostgreSQL with vector extension):

```json
{
  "mcpServers": {
    "mcp-myscaledb": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "mcp-clickhouse",
        "--python",
        "3.10",
        "mcp-clickhouse"
      ],
      "env": {
        "PGVECTOR_ENABLED": "true",
        "MYSCALE_ENABLED": "false",
        "PGVECTOR_HOST": "localhost",
        "PGVECTOR_PORT": "5432",
        "PGVECTOR_USER": "postgres",
        "PGVECTOR_PASSWORD": "postgres",
        "PGVECTOR_DATABASE": "vectordb"
      }
    }
  }
}
```

You can also enable multiple services simultaneously:

```json
{
  "mcpServers": {
    "mcp-myscaledb": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "mcp-clickhouse",
        "--python",
        "3.10",
        "mcp-clickhouse"
      ],
      "env": {
        "MYSCALE_HOST": "<myscaledb-host>",
        "MYSCALE_PORT": "<myscaledb-port>",
        "MYSCALE_USER": "<myscaledb-user>",
        "MYSCALE_PASSWORD": "<myscaledb-password>",
        "MYSCALE_SECURE": "true",
        "MYSCALE_VERIFY": "true",
        "MYSCALE_CONNECT_TIMEOUT": "30",
        "MYSCALE_SEND_RECEIVE_TIMEOUT": "30",
        "CHDB_ENABLED": "true",
        "CHDB_DATA_PATH": "/path/to/chdb/data",
        "PGVECTOR_ENABLED": "true",
        "PGVECTOR_HOST": "localhost",
        "PGVECTOR_PORT": "5432",
        "PGVECTOR_USER": "postgres",
        "PGVECTOR_PASSWORD": "postgres",
        "PGVECTOR_DATABASE": "vectordb"
      }
    }
  }
}
```

3. Locate the command entry for `uv` and replace it with the absolute path to the `uv` executable. This ensures that the correct version of `uv` is used when starting the server. On a mac, you can find this path using `which uv`.

4. Restart Claude Desktop to apply the changes.

## Development

1. In `test-services` directory run `docker compose up -d` to start the MyScaleDB cluster.

2. Add the following variables to a `.env` file in the root of the repository.

*Note: The use of the `default` user in this context is intended solely for local development purposes.*

```bash
MYSCALE_HOST=localhost
MYSCALE_PORT=8123
MYSCALE_USER=default
MYSCALE_PASSWORD=myscaledb
```

3. Run `uv sync` to install the dependencies. To install `uv` follow the instructions [here](https://docs.astral.sh/uv/). Then do `source .venv/bin/activate`.

4. For easy testing with the MCP Inspector, run `fastmcp dev mcp_server/main.py` to start the MCP server.

5. To test with HTTP transport and the health check endpoint:
   ```bash
   # Using default port 8000
   MYSCALE_MCP_SERVER_TRANSPORT=http python -m mcp_server.main

   # Or with a custom port
   MYSCALE_MCP_SERVER_TRANSPORT=http MYSCALE_MCP_BIND_PORT=4200 python -m mcp_server.main

   # Then in another terminal:
   curl http://localhost:8000/health  # or http://localhost:4200/health for custom port
   ```

### Environment Variables

The following environment variables are used to configure the MyScaleDB, chDB, and pgvector connections:

#### MyScaleDB Variables

##### Required Variables

* `MYSCALE_HOST`: The hostname of your MyScaleDB server
* `MYSCALE_USER`: The username for authentication
* `MYSCALE_PASSWORD`: The password for authentication

> [!CAUTION]
> It is important to treat your MCP database user as you would any external client connecting to your database, granting only the minimum necessary privileges required for its operation. The use of default or administrative users should be strictly avoided at all times.

##### Optional Variables

* `MYSCALE_PORT`: The port number of your MyScaleDB server
  * Default: `8443` if HTTPS is enabled, `8123` if disabled
  * Usually doesn't need to be set unless using a non-standard port
* `MYSCALE_SECURE`: Enable/disable HTTPS connection
  * Default: `"true"`
  * Set to `"false"` for non-secure connections
* `MYSCALE_VERIFY`: Enable/disable SSL certificate verification
  * Default: `"true"`
  * Set to `"false"` to disable certificate verification (not recommended for production)
  * TLS certificates: The package uses your operating system trust store for TLS certificate verification via `truststore`. We call `truststore.inject_into_ssl()` at startup to ensure proper certificate handling. Python's default SSL behavior is used as a fallback only if an unexpected error occurs.
* `MYSCALE_CONNECT_TIMEOUT`: Connection timeout in seconds
  * Default: `"30"`
  * Increase this value if you experience connection timeouts
* `MYSCALE_SEND_RECEIVE_TIMEOUT`: Send/receive timeout in seconds
  * Default: `"300"`
  * Increase this value for long-running queries
* `MYSCALE_DATABASE`: Default database to use
  * Default: None (uses server default)
  * Set this to automatically connect to a specific database
* `MYSCALE_MCP_SERVER_TRANSPORT`: Sets the transport method for the MCP server.
  * Default: `"stdio"`
  * Valid options: `"stdio"`, `"http"`, `"sse"`. This is useful for local development with tools like MCP Inspector.
* `MYSCALE_MCP_BIND_HOST`: Host to bind the MCP server to when using HTTP or SSE transport
  * Default: `"127.0.0.1"`
  * Set to `"0.0.0.0"` to bind to all network interfaces (useful for Docker or remote access)
  * Only used when transport is `"http"` or `"sse"`
* `MYSCALE_MCP_BIND_PORT`: Port to bind the MCP server to when using HTTP or SSE transport
  * Default: `"8000"`
  * Only used when transport is `"http"` or `"sse"`
* `MYSCALE_MCP_QUERY_TIMEOUT`: Timeout in seconds for SELECT tools
  * Default: `"30"`
  * Increase this if you see `Query timed out after ...` errors for heavy queries
* `MYSCALE_ENABLED`: Enable/disable MyScaleDB functionality
  * Default: `"true"`
  * Set to `"false"` to disable MyScaleDB tools when using chDB or pgvector only

#### chDB Variables

* `CHDB_ENABLED`: Enable/disable chDB functionality
  * Default: `"false"`
  * Set to `"true"` to enable chDB tools
* `CHDB_DATA_PATH`: The path to the chDB data directory
  * Default: `":memory:"` (in-memory database)
  * Use `:memory:` for in-memory database
  * Use a file path for persistent storage (e.g., `/path/to/chdb/data`)

#### pgvector Variables

* `PGVECTOR_ENABLED`: Enable/disable pgvector functionality
  * Default: `"false"`
  * Set to `"true"` to enable pgvector tools
* `PGVECTOR_HOST`: The hostname of your PostgreSQL server
* `PGVECTOR_PORT`: The port number (default: 5432)
* `PGVECTOR_USER`: The username for authentication
* `PGVECTOR_PASSWORD`: The password for authentication
* `PGVECTOR_DATABASE`: The database name
* `PGVECTOR_CONNECT_TIMEOUT`: Connection timeout in seconds (default: 30)
* `PGVECTOR_SSLMODE`: SSL mode for connection (default: prefer)

#### Example Configurations

For local development with Docker:

```env
# Required variables
MYSCALE_HOST=localhost
MYSCALE_USER=default
MYSCALE_PASSWORD=myscaledb

# Optional: Override defaults for local development
MYSCALE_SECURE=false  # Uses port 8123 automatically
MYSCALE_VERIFY=false
```

For MyScaleDB Cloud:

```env
# Required variables
MYSCALE_HOST=your-instance.myscale.cloud
MYSCALE_USER=default
MYSCALE_PASSWORD=your-password

# Optional: These use secure defaults
# MYSCALE_SECURE=true  # Uses port 8443 automatically
# MYSCALE_DATABASE=your_database
```

For chDB only (in-memory):

```env
# chDB configuration
CHDB_ENABLED=true
MYSCALE_ENABLED=false
# CHDB_DATA_PATH defaults to :memory:
```

For chDB with persistent storage:

```env
# chDB configuration
CHDB_ENABLED=true
MYSCALE_ENABLED=false
CHDB_DATA_PATH=/path/to/chdb/data
```

For pgvector only:

```env
# pgvector configuration
PGVECTOR_ENABLED=true
MYSCALE_ENABLED=false
PGVECTOR_HOST=localhost
PGVECTOR_PORT=5432
PGVECTOR_USER=postgres
PGVECTOR_PASSWORD=postgres
PGVECTOR_DATABASE=vectordb
```

For MCP Inspector or remote access with HTTP transport:

```env
MYSCALE_HOST=localhost
MYSCALE_USER=default
MYSCALE_PASSWORD=myscaledb
MYSCALE_MCP_SERVER_TRANSPORT=http
MYSCALE_MCP_BIND_HOST=0.0.0.0  # Bind to all interfaces
MYSCALE_MCP_BIND_PORT=4200  # Custom port (default: 8000)
```

When using HTTP transport, the server will run on the configured port (default 8000). For example, with the above configuration:
- MCP endpoint: `http://localhost:4200/mcp`
- Health check: `http://localhost:4200/health`

You can set these variables in your environment, in a `.env` file, or in the Claude Desktop configuration:

```json
{
  "mcpServers": {
    "mcp-myscaledb": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "mcp-clickhouse",
        "--python",
        "3.10",
        "mcp-clickhouse"
      ],
      "env": {
        "MYSCALE_HOST": "<myscaledb-host>",
        "MYSCALE_USER": "<myscaledb-user>",
        "MYSCALE_PASSWORD": "<myscaledb-password>",
        "MYSCALE_DATABASE": "<optional-database>",
        "MYSCALE_MCP_SERVER_TRANSPORT": "stdio",
        "MYSCALE_MCP_BIND_HOST": "127.0.0.1",
        "MYSCALE_MCP_BIND_PORT": "8000"
      }
    }
  }
}
```

Note: The bind host and port settings are only used when transport is set to "http" or "sse".

### Running tests

```bash
uv sync --all-extras --dev # install dev dependencies
uv run ruff check . # run linting

docker compose up -d test_services # start MyScaleDB
uv run pytest -v tests
uv run pytest -v tests/test_tool.py # MyScaleDB only
uv run pytest -v tests/test_chdb_tool.py # chDB only
uv run pytest -v tests/test_pgvector_tool.py # pgvector only
```
