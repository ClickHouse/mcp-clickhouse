# ClickHouse MCP Server

[![PyPI - Version](https://img.shields.io/pypi/v/mcp-clickhouse)](https://pypi.org/project/mcp-clickhouse)

An MCP server for ClickHouse.

<a href="https://glama.ai/mcp/servers/yvjy4csvo1"><img width="380" height="200" src="https://glama.ai/mcp/servers/yvjy4csvo1/badge" alt="mcp-clickhouse MCP server" /></a>

## Features

### ClickHouse Tools

* `run_query`
  * Execute SQL queries on your ClickHouse cluster.
  * Input: `query` (string): The SQL query to execute.
  * Queries run in read-only mode by default (`CLICKHOUSE_ALLOW_WRITE_ACCESS=false`), but writes can be enabled explicitly if needed.

* `list_databases`
  * List all databases on your ClickHouse cluster.

* `list_tables`
  * List tables in a database with pagination.
  * Required input: `database` (string).
  * Optional inputs:
    * `like` / `not_like` (string): Apply `LIKE` or `NOT LIKE` filters to table names.
    * `page_token` (string): Token returned by a previous call for fetching the next page.
    * `page_size` (int, default `50`): Number of tables returned per page.
    * `include_detailed_columns` (bool, default `true`): When `false`, omits column metadata for lighter responses while keeping the full `create_table_query`.
  * Response shape:
    * `tables`: Array of table objects for the current page.
    * `next_page_token`: Pass this value back to fetch the next page, or `null` when there are no more tables.
    * `total_tables`: Total count of tables that match the supplied filters.

### chDB Tools

* `run_chdb_select_query`
  * Execute SQL queries using [chDB](https://github.com/chdb-io/chdb)'s embedded ClickHouse engine.
  * Input: `query` (string): The SQL query to execute.
  * Query data directly from various sources (files, URLs, databases) without ETL processes.
  * Requires the optional `chdb` extra: `pip install 'mcp-clickhouse[chdb]'`

### Health Check Endpoint

When running with HTTP or SSE transport, a health check endpoint is available at `/health`. This endpoint:
- Returns `200 OK` with the ClickHouse version if the server is healthy and can connect to ClickHouse
- Returns `503 Service Unavailable` if the server cannot connect to ClickHouse

Example:
```bash
curl http://localhost:8000/health
# Response: OK - Connected to ClickHouse 24.3.1
```

## Security

### Authentication for HTTP/SSE Transports

When using HTTP or SSE transport, authentication is **required by default**. The `stdio` transport (default) does not require authentication as it only communicates via standard input/output.

#### Setting Up Authentication

1. Generate a secure token (can be any random string):
   ```bash
   # Using uuidgen (macOS/Linux)
   uuidgen

   # Using openssl
   openssl rand -hex 32
   ```

2. Configure the server with the token:
   ```bash
   export CLICKHOUSE_MCP_AUTH_TOKEN="your-generated-token"
   ```

3. Configure your MCP client to include the token in requests:

   For Claude Desktop with HTTP/SSE transport:
   ```json
   {
     "mcpServers": {
       "mcp-clickhouse": {
         "url": "http://127.0.0.1:8000",
         "headers": {
           "Authorization": "Bearer your-generated-token"
         }
       }
     }
   }
   ```

   For command-line tools:
   ```bash
   curl -H "Authorization: Bearer your-generated-token" http://localhost:8000/health
   ```

#### Development Mode (Disabling Authentication)

For local development and testing only, you can disable authentication by setting:
```bash
export CLICKHOUSE_MCP_AUTH_DISABLED=true
```

**WARNING:** Only use this for local development. Do not disable authentication when the server is exposed to any network.

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
        "3.10",
        "mcp-clickhouse"
      ],
      "env": {
        "CLICKHOUSE_HOST": "<clickhouse-host>",
        "CLICKHOUSE_PORT": "<clickhouse-port>",
        "CLICKHOUSE_USER": "<clickhouse-user>",
        "CLICKHOUSE_PASSWORD": "<clickhouse-password>",
        "CLICKHOUSE_ROLE": "<clickhouse-role>",
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
        "3.10",
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

For chDB (embedded ClickHouse engine), add the following configuration:

```json
{
  "mcpServers": {
    "mcp-clickhouse": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "mcp-clickhouse[chdb]",
        "--python",
        "3.10",
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
        "mcp-clickhouse[chdb]",
        "--python",
        "3.10",
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

### Optional Write Access

By default, this MCP enforces read-only queries so that accidental mutations cannot happen during exploration. To allow DDL or INSERT/UPDATE statements, set the `CLICKHOUSE_ALLOW_WRITE_ACCESS` environment variable to `true`. The server keeps enforcing read-only mode if the ClickHouse instance itself disallows writes.

### Destructive Operation Protection

Even when write access is enabled (`CLICKHOUSE_ALLOW_WRITE_ACCESS=true`), destructive operations (DROP TABLE, DROP DATABASE, DROP VIEW, DROP DICTIONARY, TRUNCATE TABLE) require an additional opt-in flag for safety. This prevents accidental data deletion during AI exploration.

To enable destructive operations, set both flags:
```json
"env": {
  "CLICKHOUSE_ALLOW_WRITE_ACCESS": "true",
  "CLICKHOUSE_ALLOW_DROP": "true"
}
```

This two-tier approach ensures that accidental drops are very difficult:
- **Write operations** (INSERT, UPDATE, CREATE) require `CLICKHOUSE_ALLOW_WRITE_ACCESS=true`
- **Destructive operations** (DROP, TRUNCATE) additionally require `CLICKHOUSE_ALLOW_DROP=true`

### Running Without uv (Using System Python)

If you prefer to use the system Python installation instead of uv, you can install the package from PyPI and run it directly:

1. Install the package using pip:
   ```bash
   python3 -m pip install mcp-clickhouse
   ```

   To install chDB support as well:
   ```bash
   python3 -m pip install 'mcp-clickhouse[chdb]'
   ```

   To upgrade to the latest version:
   ```bash
   python3 -m pip install --upgrade mcp-clickhouse
   ```

2. Update your Claude Desktop configuration to use Python directly:

```json
{
  "mcpServers": {
    "mcp-clickhouse": {
      "command": "python3",
      "args": [
        "-m",
        "mcp_clickhouse.main"
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

Alternatively, you can use the installed script directly:

```json
{
  "mcpServers": {
    "mcp-clickhouse": {
      "command": "mcp-clickhouse",
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

Note: Make sure to use the full path to the Python executable or the `mcp-clickhouse` script if they are not in your system PATH. You can find the paths using:
- `which python3` for the Python executable
- `which mcp-clickhouse` for the installed script

## Custom Middleware

You can add custom middleware to the MCP server without modifying the source code. FastMCP provides a middleware system that allows you to intercept and process MCP protocol messages (tool calls, resource reads, prompts, etc.).

### How to Use

1. Create a Python module with middleware classes extending `Middleware` and a `setup_middleware(mcp)` function:

```python
# my_middleware.py
import logging
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext

logger = logging.getLogger("my-middleware")

class LoggingMiddleware(Middleware):
    """Log all tool calls."""
    
    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        tool_name = context.message.name if hasattr(context.message, 'name') else 'unknown'
        logger.info(f"Calling tool: {tool_name}")
        result = await call_next(context)
        logger.info(f"Tool {tool_name} completed")
        return result

def setup_middleware(mcp):
    """Register middleware with the MCP server."""
    mcp.add_middleware(LoggingMiddleware())
```

2. Set the `MCP_MIDDLEWARE_MODULE` environment variable to the module name (without `.py` extension):

```json
{
  "mcpServers": {
    "mcp-clickhouse": {
      "command": "uv",
      "args": ["run", "--with", "mcp-clickhouse", "--python", "3.10", "mcp-clickhouse"],
      "env": {
        "CLICKHOUSE_HOST": "<clickhouse-host>",
        "CLICKHOUSE_USER": "<clickhouse-user>",
        "CLICKHOUSE_PASSWORD": "<clickhouse-password>",
        "MCP_MIDDLEWARE_MODULE": "my_middleware"
      }
    }
  }
}
```

3. Ensure your middleware module is in Python's import path (e.g., in the same directory where the MCP server runs, or installed as a package).

### Example Middleware

An example middleware module is provided in `example_middleware.py` showing common patterns:
- Logging all MCP requests
- Logging tool calls specifically
- Measuring request processing time

To use the example:
```json
"env": {
  "MCP_MIDDLEWARE_MODULE": "example_middleware"
}
```

### Middleware Capabilities

The `Middleware` base class provides hooks for different MCP operations:

- `on_message(context, call_next)` - Called for all messages
- `on_request(context, call_next)` - Called for all requests
- `on_notification(context, call_next)` - Called for all notifications
- `on_call_tool(context, call_next)` - Called when a tool is executed
- `on_read_resource(context, call_next)` - Called when a resource is read
- `on_get_prompt(context, call_next)` - Called when a prompt is retrieved
- `on_list_tools(context, call_next)` - Called when listing tools
- `on_list_resources(context, call_next)` - Called when listing resources
- `on_list_resource_templates(context, call_next)` - Called when listing resource templates
- `on_list_prompts(context, call_next)` - Called when listing prompts

Each hook receives a `MiddlewareContext` object containing the message and metadata, and a `call_next` function to continue the pipeline.

### Dynamic Client Configuration via Context State

Middleware can override ClickHouse client configuration on a per-request basis using the `CLIENT_CONFIG_OVERRIDES_KEY` context state key. The server merges these overrides with the base configuration from environment variables.

```python
from fastmcp.server.dependencies import get_context
from mcp_clickhouse.mcp_server import CLIENT_CONFIG_OVERRIDES_KEY

ctx = get_context()
ctx.set_state(CLIENT_CONFIG_OVERRIDES_KEY, {
    "connect_timeout": 60,
    "send_receive_timeout": 120
})
```

This enables advanced use cases like dynamic timeout adjustments, tenant-specific routing, or per-user connection settings.

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

5. To test with HTTP transport and the health check endpoint:
   ```bash
   # For development, disable authentication
   CLICKHOUSE_MCP_SERVER_TRANSPORT=http CLICKHOUSE_MCP_AUTH_DISABLED=true python -m mcp_clickhouse.main

   # Or with authentication (generate a token first)
   CLICKHOUSE_MCP_SERVER_TRANSPORT=http CLICKHOUSE_MCP_AUTH_TOKEN="your-token" python -m mcp_clickhouse.main

   # Then in another terminal:
   # Without auth (if disabled):
   curl http://localhost:8000/health

   # With auth:
   curl -H "Authorization: Bearer your-token" http://localhost:8000/health
   ```

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
* `CLICKHOUSE_ROLE`: The role to use for authentication
  * Default: None
  * Set this if your user requires a specific role
* `CLICKHOUSE_SECURE`: Enable/disable HTTPS connection
  * Default: `"true"`
  * Set to `"false"` for non-secure connections
* `CLICKHOUSE_VERIFY`: Enable/disable SSL certificate verification
  * Default: `"true"`
  * Set to `"false"` to disable certificate verification (not recommended for production)
  * TLS certificates: The package uses your operating system trust store for TLS certificate verification via `truststore`. We call `truststore.inject_into_ssl()` at startup to ensure proper certificate handling. Python’s default SSL behavior is used as a fallback only if an unexpected error occurs.
* `CLICKHOUSE_SERVER_HOST_NAME`: Server hostname for SNI override and certificate validation
  * Default: None (uses the connection hostname)
  * This is useful when connecting through proxies or load balancers where the certificate hostname differs from the connection hostname. When set, this hostname will be used for both SNI (Server Name Indication) during the TLS handshake and for certificate hostname validation.
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
  * Valid options: `"stdio"`, `"http"`, `"sse"`. This is useful for local development with tools like MCP Inspector.
* `CLICKHOUSE_MCP_BIND_HOST`: Host to bind the MCP server to when using HTTP or SSE transport
  * Default: `"127.0.0.1"`
  * Set to `"0.0.0.0"` to bind to all network interfaces (useful for Docker or remote access)
  * Only used when transport is `"http"` or `"sse"`
* `CLICKHOUSE_MCP_BIND_PORT`: Port to bind the MCP server to when using HTTP or SSE transport
  * Default: `"8000"`
  * Only used when transport is `"http"` or `"sse"`
* `CLICKHOUSE_MCP_QUERY_TIMEOUT`: Timeout in seconds for SELECT tools
  * Default: `"30"`
  * Increase this if you see `Query timed out after ...` errors for heavy queries
* `CLICKHOUSE_MCP_AUTH_TOKEN`: Authentication token for HTTP/SSE transports
  * Default: None
  * **Required** when using HTTP or SSE transport (unless `CLICKHOUSE_MCP_AUTH_DISABLED=true`)
  * Generate using `uuidgen` or `openssl rand -hex 32`
  * Clients must send this token in the `Authorization: Bearer <token>` header
* `CLICKHOUSE_MCP_AUTH_DISABLED`: Disable authentication for HTTP/SSE transports
  * Default: `"false"` (authentication is enabled)
  * Set to `"true"` to disable authentication for local development/testing only
  * **WARNING:** Only use for local development. Do not disable when exposed to networks
* `CLICKHOUSE_ENABLED`: Enable/disable ClickHouse functionality
  * Default: `"true"`
  * Set to `"false"` to disable ClickHouse tools when using chDB only
* `CLICKHOUSE_ALLOW_WRITE_ACCESS`: Allow write operations (DDL and DML)
  * Default: `"false"`
  * Set to `"true"` to allow DDL (CREATE, ALTER, DROP) and DML (INSERT, UPDATE, DELETE) operations
  * When disabled (default), queries run with `readonly=1` setting to prevent data modifications
* `CLICKHOUSE_ALLOW_DROP`: Allow destructive operations (DROP TABLE, DROP DATABASE, DROP VIEW, DROP DICTIONARY, TRUNCATE TABLE)
  * Default: `"false"`
  * Only takes effect when `CLICKHOUSE_ALLOW_WRITE_ACCESS=true` is also set
  * Set to `"true"` to explicitly allow destructive DROP and TRUNCATE operations
  * This is a safety feature to prevent accidental data deletion during AI exploration

#### Middleware Variables

* `MCP_MIDDLEWARE_MODULE`: Python module name containing custom middleware to inject into the MCP server
  * Default: None (no middleware loaded)
  * Set to the module name (without `.py` extension) of your middleware module
  * The module must provide a `setup_middleware(mcp)` function
  * See [Custom Middleware](#custom-middleware) for details and examples

#### chDB Variables

* `CHDB_ENABLED`: Enable/disable chDB functionality
  * Default: `"false"`
  * Set to `"true"` to enable chDB tools
  * Requires installing the optional extra: `mcp-clickhouse[chdb]`
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

For MCP Inspector or remote access with HTTP transport:

```env
CLICKHOUSE_HOST=localhost
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=clickhouse
CLICKHOUSE_MCP_SERVER_TRANSPORT=http
CLICKHOUSE_MCP_BIND_HOST=0.0.0.0  # Bind to all interfaces
CLICKHOUSE_MCP_BIND_PORT=4200  # Custom port (default: 8000)
CLICKHOUSE_MCP_AUTH_TOKEN=your-generated-token  # Required for HTTP/SSE
```

For local development with HTTP transport (authentication disabled):

```env
CLICKHOUSE_HOST=localhost
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=clickhouse
CLICKHOUSE_MCP_SERVER_TRANSPORT=http
CLICKHOUSE_MCP_AUTH_DISABLED=true  # Only for local development!
```

When using HTTP transport, the server will run on the configured port (default 8000). For example, with the above configuration:
- MCP endpoint: `http://localhost:4200/mcp`
- Health check: `http://localhost:4200/health`

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
        "3.10",
        "mcp-clickhouse"
      ],
      "env": {
        "CLICKHOUSE_HOST": "<clickhouse-host>",
        "CLICKHOUSE_USER": "<clickhouse-user>",
        "CLICKHOUSE_PASSWORD": "<clickhouse-password>",
        "CLICKHOUSE_DATABASE": "<optional-database>",
        "CLICKHOUSE_MCP_SERVER_TRANSPORT": "stdio",
        "CLICKHOUSE_MCP_BIND_HOST": "127.0.0.1",
        "CLICKHOUSE_MCP_BIND_PORT": "8000"
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

docker compose up -d test_services # start ClickHouse
uv run pytest -v tests
uv run pytest -v tests/test_tool.py # ClickHouse only
CHDB_ENABLED=true uv run --extra chdb pytest -v tests/test_chdb_tool.py # chDB only
```

## YouTube Overview

[![YouTube](http://i.ytimg.com/vi/y9biAm_Fkqw/hqdefault.jpg)](https://www.youtube.com/watch?v=y9biAm_Fkqw)
