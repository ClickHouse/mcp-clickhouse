# ClickHouse MCP Server

<!-- mcp-name: io.github.ClickHouse/mcp-clickhouse -->

[![PyPI - Version](https://img.shields.io/pypi/v/mcp-clickhouse)](https://pypi.org/project/mcp-clickhouse)

An MCP server for ClickHouse.

<a href="https://glama.ai/mcp/servers/yvjy4csvo1"><img width="380" height="200" src="https://glama.ai/mcp/servers/yvjy4csvo1/badge" alt="mcp-clickhouse MCP server" /></a>

The server implements MCP `2026-07-28` and supports legacy initialize handshakes from
`2024-11-05` through `2025-11-25`. Modern clients use sessionless requests and
`server/discover`. Existing clients can continue to negotiate the legacy protocol.

> [!NOTE]
> HTTP requests without `MCP-Protocol-Version` are routed through legacy handling so
> clients from before `2025-06-18` can continue to connect. MCP `2026-07-28` permits
> this behavior on servers that support those clients. Modern clients should send the
> header on every POST request.

## Features

### ClickHouse Tools

ClickHouse tool responses are JSON-encoded strings. Integers outside
`[-9007199254740991, 9007199254740991]` are returned as decimal strings to preserve exact
values in JavaScript clients. This applies to query rows and integer table metadata. Safe-range
integers and booleans keep their JSON types.

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
    * `page_token` (string): Single-use token returned by a previous call. It is retained for up to one hour.
    * `page_size` (int, default `50`): Number of tables returned per page; must be greater than `0`.
    * `include_detailed_columns` (bool, default `true`): When `false`, omits column metadata for lighter responses while keeping the full `create_table_query`.
  * Response shape:
    * `tables`: Array of table objects for the current page.
    * `next_page_token`: Pass this single-use value back before it expires to fetch the next page, or `null` when there are no more tables.
    * `total_tables`: Total count of tables that match the supplied filters.

### chDB Tools

* `run_chdb_select_query`
  * Execute SQL queries using [chDB](https://github.com/chdb-io/chdb)'s embedded ClickHouse engine.
  * Input: `query` (string): The SQL query to execute.
  * Integers outside `[-9007199254740991, 9007199254740991]` are returned as decimal strings.
  * Query data directly from various sources (files, URLs, databases) without ETL processes.
  * Requires the optional `chdb` extra: `pip install 'mcp-clickhouse[chdb]'`

### Health Check Endpoint

When running with HTTP or SSE transport, a health check endpoint is available at `/health`. This endpoint:
- Returns `200 OK` (body: `OK`) if the server is healthy and can connect to ClickHouse
- Returns `503 Service Unavailable` with a generic error message if the server cannot connect to ClickHouse
- Returns `503` if a ClickHouse probe does not finish within two seconds. Concurrent requests share one in-flight probe

GET and HEAD requests to the endpoint are intentionally unauthenticated and exempt from Host and Origin validation so orchestrator probes (e.g. Kubernetes liveness/readiness, load balancers) can use runtime-assigned pod or target IPs without extra configuration. `/health` is reserved and cannot be used as the MCP transport path. The response body is deliberately minimal to avoid leaking backend version strings or error details; debug failures via the server logs.

Example:
```bash
curl http://localhost:8000/health
# Response: OK
```

## Security

### Authentication for HTTP/SSE Transports

When using HTTP or SSE transport, authentication is **required by default**. The `stdio` transport (default) does not require authentication as it only communicates via standard input/output.

Three authentication modes are supported. Pick one:

| Mode                       | When to use                               | Env var                                                                                        |
|----------------------------|-------------------------------------------|------------------------------------------------------------------------------------------------|
| Static bearer token        | Simple deployments, internal services     | `CLICKHOUSE_MCP_AUTH_TOKEN`                                                                    |
| OAuth / OIDC (via FastMCP) | Azure Entra, Google, GitHub, WorkOS, etc. | `FASTMCP_SERVER_AUTH=<provider-class-path>` (+ provider-specific `FASTMCP_SERVER_AUTH_*` vars) |
| Disabled                   | Local development only                    | `CLICKHOUSE_MCP_AUTH_DISABLED=true`                                                            |

Startup fails if none of these are configured for HTTP/SSE transports.

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

   Note: the `/health` endpoint is intentionally unauthenticated (see [Health Check Endpoint](#health-check-endpoint) above). To verify that bearer-token auth is actually rejecting unauthenticated requests, hit the MCP endpoint itself e.g. with the MCP Inspector, or by POSTing a JSON-RPC request to `/mcp` with and without the `Authorization` header and confirming the unauthenticated call returns `401`.

#### OAuth / OIDC via FastMCP

For production deployments with identity providers (Azure Entra, Google, GitHub, WorkOS, etc.), delegate authentication to [FastMCP's built-in auth providers](https://gofastmcp.com/servers/auth) instead of using a static token. Set `FASTMCP_SERVER_AUTH` to the **full class path** of a FastMCP auth provider, along with the provider-specific `FASTMCP_SERVER_AUTH_*` variables, and leave `CLICKHOUSE_MCP_AUTH_TOKEN` unset.

Example (Azure Entra):

```bash
export FASTMCP_SERVER_AUTH=fastmcp.server.auth.providers.azure.AzureProvider
export FASTMCP_SERVER_AUTH_AZURE_TENANT_ID="<tenant-id>"
export FASTMCP_SERVER_AUTH_AZURE_CLIENT_ID="<client-id>"
export FASTMCP_SERVER_AUTH_AZURE_CLIENT_SECRET="<client-secret>"
export FASTMCP_SERVER_AUTH_AZURE_BASE_URL="https://mcp.example.com"
export FASTMCP_SERVER_AUTH_AZURE_REQUIRED_SCOPES="read access_as_user"
```

mcp-clickhouse retains these FastMCP 2.14.7 environment prefixes for the FastMCP 4.0.0
built-in providers:

| Provider class path | Provider variable prefix |
|---------------------|--------------------------|
| `fastmcp.server.auth.providers.auth0.Auth0Provider` | `FASTMCP_SERVER_AUTH_AUTH0_` |
| `fastmcp.server.auth.providers.aws.AWSCognitoProvider` | `FASTMCP_SERVER_AUTH_AWS_COGNITO_` |
| `fastmcp.server.auth.providers.azure.AzureProvider` | `FASTMCP_SERVER_AUTH_AZURE_` |
| `fastmcp.server.auth.providers.descope.DescopeProvider` | `FASTMCP_SERVER_AUTH_DESCOPEPROVIDER_` |
| `fastmcp.server.auth.providers.discord.DiscordProvider` | `FASTMCP_SERVER_AUTH_DISCORD_` |
| `fastmcp.server.auth.providers.github.GitHubProvider` | `FASTMCP_SERVER_AUTH_GITHUB_` |
| `fastmcp.server.auth.providers.google.GoogleProvider` | `FASTMCP_SERVER_AUTH_GOOGLE_` |
| `fastmcp.server.auth.providers.introspection.IntrospectionTokenVerifier` | `FASTMCP_SERVER_AUTH_INTROSPECTION_` |
| `fastmcp.server.auth.providers.jwt.JWTVerifier` | `FASTMCP_SERVER_AUTH_JWT_` |
| `fastmcp.server.auth.providers.oci.OCIProvider` | `FASTMCP_SERVER_AUTH_OCI_` |
| `fastmcp.server.auth.providers.scalekit.ScalekitProvider` | `FASTMCP_SERVER_AUTH_SCALEKITPROVIDER_` |
| `fastmcp.server.auth.providers.supabase.SupabaseProvider` | `FASTMCP_SERVER_AUTH_SUPABASE_` |
| `fastmcp.server.auth.providers.workos.WorkOSProvider` | `FASTMCP_SERVER_AUTH_WORKOS_` |
| `fastmcp.server.auth.providers.workos.AuthKitProvider` | `FASTMCP_SERVER_AUTH_AUTHKITPROVIDER_` |

Append the uppercase provider field name to the prefix. See the
[FastMCP docs](https://gofastmcp.com/servers/auth) for each provider's configuration
requirements.

Auth values set directly in the process environment take precedence case-insensitively.
The default `.env` load starts at the installed `mcp_clickhouse` package directory,
resolves symlinks first, and walks upward to the filesystem root. It loads the first
`.env` it finds and loads nothing if there is none. It never reads the working
directory, regardless of how the server is launched. A source checkout normally finds
the repository root `.env`. That file may also provide `FASTMCP_SERVER_AUTH` and its
provider fields. Its values take precedence over the explicit or compatibility auth
file.
For FastMCP 2 compatibility, mcp-clickhouse reads missing provider fields from `.env`
in the working directory, but that compatibility fallback cannot select
`FASTMCP_SERVER_AUTH`. A process-set `FASTMCP_ENV_FILE` replaces that compatibility
fallback and may provide both the selector and provider fields. Set it before startup.
The mcp-clickhouse compatibility loader reads only `FASTMCP_SERVER_AUTH` and
`FASTMCP_SERVER_AUTH_*` from that file, so it cannot inject `CLICKHOUSE_*` settings.
FastMCP 4 may use the same file for its own broader settings. A custom provider receives
no environment-derived constructor arguments and must support no-argument construction.

Treat both discovered and working-directory `.env` files as trusted authentication
configuration. Anyone who can create or write a `.env` in any directory from the package
directory up to the filesystem root can control which file is discovered, select the
provider, and set its fields. Anyone who can write the working-directory file controls every provider
field absent from the process and discovered configuration, including signing keys,
issuers and endpoints, and client secrets. A process-set `FASTMCP_ENV_FILE` that points
to an operator-owned file disables the working-directory fallback.

FastMCP 4 changed the default OAuth proxy client store. Deployments that relied on
FastMCP 2's default OAuth proxy storage must have clients register and authorize again.
Compatible custom storage, static bearer tokens, and JWT verification are unaffected.

#### Development Mode (Disabling Authentication)

For local development and testing only, you can disable authentication by setting:
```bash
export CLICKHOUSE_MCP_AUTH_DISABLED=true
export CLICKHOUSE_MCP_ALLOWED_HOSTS=127.0.0.1:8000,localhost:8000
```

**WARNING:** Only use this for local development. Do not disable authentication when the server is exposed to any network.

## Configuration

This MCP server supports both ClickHouse and chDB. You can enable either or both depending on your needs.
Python 3.10 through 3.14 are supported. Python 3.12 is recommended for local launches.

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
        "3.12",
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
        "CLICKHOUSE_CONNECT_TIMEOUT": "30"
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
        "3.12",
        "mcp-clickhouse"
      ],
      "env": {
        "CLICKHOUSE_HOST": "sql-clickhouse.clickhouse.com",
        "CLICKHOUSE_PORT": "8443",
        "CLICKHOUSE_USER": "demo",
        "CLICKHOUSE_PASSWORD": "",
        "CLICKHOUSE_SECURE": "true",
        "CLICKHOUSE_VERIFY": "true",
        "CLICKHOUSE_CONNECT_TIMEOUT": "30"
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
        "3.12",
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
        "3.12",
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

By default, this MCP enforces read-only queries so that accidental mutations cannot happen during exploration. To allow DDL or INSERT statements, set the `CLICKHOUSE_ALLOW_WRITE_ACCESS` environment variable to `true`. The server keeps enforcing read-only mode if the ClickHouse instance itself disallows writes.

### Destructive Operation Protection

Even when write access is enabled (`CLICKHOUSE_ALLOW_WRITE_ACCESS=true`), destructive operations require an additional opt-in flag for safety. The check covers any `DROP` statement (including the `ALTER TABLE ... DROP PARTITION` / `DROP PART` / `DROP COLUMN` clauses), any `TRUNCATE`, `DELETE` and `UPDATE` (both the lightweight statements and the `ALTER TABLE ... DELETE` / `ALTER TABLE ... UPDATE` mutations), `REPLACE TABLE`, `CREATE OR REPLACE`, `ALTER TABLE ... REPLACE PARTITION`, `ALTER TABLE ... CLEAR COLUMN` / `CLEAR INDEX` / `CLEAR PROJECTION`, and `DETACH ... PERMANENTLY`. Keywords inside string literals, quoted identifiers, and SQL comments are ignored, so they neither trigger the check nor hide a statement from it.

This check runs in the MCP server and is a best-effort guard against accidents. It is not a security boundary. The security boundary is the ClickHouse user's grants. Read-only mode (the default) is enforced server-side via `readonly=1`. The destructive-operation gate is not server-enforced.

For write mode, give the MCP server a dedicated ClickHouse user with only the privileges it needs:

```sql
CREATE USER mcp_agent IDENTIFIED BY '...';
GRANT SELECT, INSERT, CREATE TABLE, ALTER ADD COLUMN ON mydb.* TO mcp_agent;
```

Every statement outside these grants then fails server-side with `ACCESS_DENIED`, regardless of MCP flags. The server settings `max_table_size_to_drop` and `max_partition_size_to_drop` can also cap blast radius if pinned with settings constraints.

To enable destructive operations, set both flags:
```json
"env": {
  "CLICKHOUSE_ALLOW_WRITE_ACCESS": "true",
  "CLICKHOUSE_ALLOW_DROP": "true"
}
```

This two-tier approach makes accidental deletion difficult:
- **Write operations** (INSERT, CREATE, ALTER ADD COLUMN) require `CLICKHOUSE_ALLOW_WRITE_ACCESS=true`
- **Destructive operations** (DROP, TRUNCATE, DELETE, UPDATE, and the rest of the list above) additionally require `CLICKHOUSE_ALLOW_DROP=true`

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
        "CLICKHOUSE_CONNECT_TIMEOUT": "30"
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
        "CLICKHOUSE_CONNECT_TIMEOUT": "30"
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
      "args": ["run", "--with", "mcp-clickhouse", "--python", "3.12", "mcp-clickhouse"],
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
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp_clickhouse.mcp_server import CLIENT_CONFIG_OVERRIDES_KEY


class ClientConfigMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        ctx = get_context()
        await ctx.set_state(
            CLIENT_CONFIG_OVERRIDES_KEY,
            {
                "connect_timeout": 60,
                "send_receive_timeout": 120,
            },
            serializable=False,
        )
        return await call_next(context)
```

This enables advanced use cases like dynamic timeout adjustments, tenant-specific routing, or per-user connection settings.

The state value must be a dictionary. Nested `settings` and `generic_args` values must be
mappings and are merged with the base configuration. Invalid values fail the tool call before
a ClickHouse client is created. `CLICKHOUSE_ROLE` remains active unless the override explicitly
supplies `settings.role`. Top-level `role` and `ch_role` keys, plus the same keys under
`generic_args`, are rejected.

Treat these overrides as trusted middleware input. Middleware must authenticate and authorize
request-derived values before setting them. Use `serializable=False` so FastMCP keeps the
value in request-local state. The default `serializable=True` stores session state and is
rejected by the server. The server snapshots the value before dispatching blocking database
work. Do not store tenant data in session-scoped Context state. A rejected session-scoped
override remains attached to a legacy MCP session and causes later tool calls in that session
to fail until the client reconnects. A per-request ClickHouse role is connection configuration,
not a tenant authorization boundary. Enforce tenant isolation with ClickHouse users, roles,
and grants.

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

4. For easy testing with the MCP Inspector, run `uv run fastmcp dev inspector mcp_clickhouse/mcp_server.py:mcp` to start the MCP server.

5. To test with HTTP transport and the health check endpoint:
   ```bash
   # For development, disable authentication
   CLICKHOUSE_MCP_SERVER_TRANSPORT=http CLICKHOUSE_MCP_AUTH_DISABLED=true CLICKHOUSE_MCP_ALLOWED_HOSTS=127.0.0.1:8000,localhost:8000 python -m mcp_clickhouse.main

   # Or with authentication (generate a token first)
   CLICKHOUSE_MCP_SERVER_TRANSPORT=http CLICKHOUSE_MCP_AUTH_TOKEN="your-token" python -m mcp_clickhouse.main

   # Then in another terminal:
   curl http://localhost:8000/health
   ```

### Environment Variables

Configuration is split into **independent** groups. Mixing them up is a common cause of hard-to-debug connection failures:

| Group | Variables | Controls |
|-------|-----------|----------|
| **ClickHouse database connection** | `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_SECURE`, `CLICKHOUSE_VERIFY`, … | How **this MCP server** connects to your ClickHouse cluster over the **HTTP interface** |
| **MCP server / transport** | `CLICKHOUSE_MCP_*`, `FASTMCP_SERVER_AUTH`, `FASTMCP_SERVER_AUTH_*`, `FASTMCP_ENV_FILE` | MCP transport, authentication, and query-tool execution limits |
| **Middleware / chDB** | `MCP_MIDDLEWARE_MODULE`, `CHDB_*` | Optional extensions |

> [!IMPORTANT]
> Variables such as `CLICKHOUSE_SECURE`, `CLICKHOUSE_VERIFY`, and `CLICKHOUSE_PORT` apply to the **ClickHouse database** connection only. They do **not** configure TLS, ports, or auth for the MCP protocol endpoint.
>
> Example: if the MCP server runs in Kubernetes behind an ingress that terminates TLS, that is an **MCP transport** concern. Keep `CLICKHOUSE_SECURE` aligned with how the pod reaches ClickHouse itself (HTTPS → `true`, plain HTTP → `false`). Setting `CLICKHOUSE_SECURE=false` because the MCP server is behind an ingress will make the server dial ClickHouse over HTTP—often against an HTTPS-only port—and produce opaque HTTP/TLS errors in the server logs.

#### ClickHouse database connection

These variables configure the [clickhouse-connect](https://clickhouse.com/docs/en/integrations/python) HTTP client and the behavior of ClickHouse-backed tools such as `run_query`, `list_databases`, and `list_tables`.
mcp-clickhouse requires clickhouse-connect 1.0.0 or newer.

##### Required Variables

* `CLICKHOUSE_HOST`: The hostname of your ClickHouse server (database endpoint, not the MCP server bind address)
* `CLICKHOUSE_USER`: The username for **ClickHouse** authentication
* `CLICKHOUSE_PASSWORD`: The password for **ClickHouse** authentication

> [!CAUTION]
> It is important to treat your MCP database user as you would any external client connecting to your database, granting only the minimum necessary privileges required for its operation. The use of default or administrative users should be strictly avoided at all times.

##### Optional Variables

* `CLICKHOUSE_PORT`: HTTP interface port of your ClickHouse server
  * Default: `8443` if `CLICKHOUSE_SECURE=true`, `8123` if `CLICKHOUSE_SECURE=false`
  * Usually doesn't need to be set unless using a non-standard port
  * **Must be an HTTP interface port**, not the native TCP protocol port used by `clickhouse-client`
  * Common values:
    * HTTP: `8123` (plain) / `8443` (TLS) — used by this server and ClickHouse Cloud HTTPS
    * Native TCP (not supported here): `9000` (plain) / `9440` (TLS) — used by `clickhouse-client`
  * If the server responds with `Port 9000 is for clickhouse-client program`, you are pointed at the native protocol; switch to the HTTP port (`8123`/`8443` or your deployment's HTTP mapping)
* `CLICKHOUSE_ROLE`: The ClickHouse role to use for authentication
  * Default: None
  * Set this if your user requires a specific role
* `CLICKHOUSE_SECURE`: Enable HTTPS **for the ClickHouse database connection** (not for MCP clients)
  * Default: `"true"`
  * Set to `"false"` only when the MCP server reaches ClickHouse over plain HTTP (typical for local Docker Compose on port `8123`)
  * Leave `"true"` for ClickHouse Cloud and any HTTPS database endpoint—even if the MCP server itself is exposed via HTTP, stdio, or an ingress that terminates TLS separately
  * Mismatching this flag with the database port (e.g. `CLICKHOUSE_SECURE=false` against port `8443`) is a frequent setup mistake and usually surfaces as confusing HTTP client errors rather than a clear "wrong scheme" message
* `CLICKHOUSE_VERIFY`: Enable/disable SSL certificate verification for the **ClickHouse** HTTPS connection
  * Default: `"true"`
  * Set to `"false"` to disable certificate verification (not recommended for production)
  * TLS certificates: The package uses your operating system trust store for TLS certificate verification via `truststore`. We call `truststore.inject_into_ssl()` at startup to ensure proper certificate handling. Python’s default SSL behavior is used as a fallback only if an unexpected error occurs.
* `CLICKHOUSE_SERVER_HOST_NAME`: Server hostname for SNI override and certificate validation on the **ClickHouse** connection
  * Default: None (uses the connection hostname)
  * This is useful when connecting through proxies or load balancers where the certificate hostname differs from the connection hostname. When set, this hostname will be used for both SNI (Server Name Indication) during the TLS handshake and for certificate hostname validation.
* `CLICKHOUSE_PROXY_PATH`: URL path prefix for the ClickHouse HTTP endpoint
  * Default: None
  * Set this when the ClickHouse HTTP interface is exposed behind a reverse proxy under a path prefix (for example, `/clickhouse`)
* `CLICKHOUSE_CONNECT_TIMEOUT`: Connection timeout in seconds for the **ClickHouse** client
  * Default: `"30"`
  * Increase this value if you experience connection timeouts
* `CLICKHOUSE_SEND_RECEIVE_TIMEOUT`: Send/receive timeout in seconds for the **ClickHouse** client
  * Default: the lower of `300` or `CLICKHOUSE_MCP_QUERY_TIMEOUT + 5`, so worker threads unblock shortly after a query timeout
  * If explicitly set, the value is used as-is (e.g. `"300"` for long-running queries)
* `CLICKHOUSE_DATABASE`: Default ClickHouse database to use
  * Default: None (uses server default)
  * Set this to automatically connect to a specific database
* `CLICKHOUSE_ENABLED`: Enable/disable ClickHouse database tools
  * Default: `"true"`
  * Set to `"false"` to disable ClickHouse tools when using chDB only
* `CLICKHOUSE_ALLOW_WRITE_ACCESS`: Allow write operations (DDL and DML) against ClickHouse
  * Default: `"false"`
  * Set to `"true"` to allow non-destructive DDL and DML (CREATE, INSERT, ALTER ADD COLUMN). Destructive statements additionally need `CLICKHOUSE_ALLOW_DROP=true`
  * When disabled (default), queries run with `readonly=1` setting to prevent data modifications
* `CLICKHOUSE_ALLOW_DROP`: Allow destructive operations (any `DROP` or `TRUNCATE`, `DELETE` and `UPDATE` including the `ALTER TABLE` variants, `REPLACE TABLE` / `REPLACE PARTITION` / `CREATE OR REPLACE`, `CLEAR COLUMN` / `CLEAR INDEX` / `CLEAR PROJECTION`, and `DETACH ... PERMANENTLY`)
  * Default: `"false"`
  * Only takes effect when `CLICKHOUSE_ALLOW_WRITE_ACCESS=true` is also set
  * This gate is a best-effort accident guard in the MCP server, not a security boundary. Restrict the ClickHouse user's grants for real enforcement (see [Destructive Operation Protection](#destructive-operation-protection))

#### MCP server and transport

These variables control the MCP process itself, including transport, authentication, and query-tool execution limits. They are independent of the ClickHouse database settings above. See also [Authentication for HTTP/SSE Transports](#authentication-for-httpsse-transports).

* `CLICKHOUSE_MCP_SERVER_TRANSPORT`: Sets the transport method for the MCP server
  * Default: `"stdio"`
  * Valid options: `"stdio"`, `"http"`, `"sse"`. This is useful for local development with tools like MCP Inspector.
  * `stdio` is typical for Claude Desktop; `http`/`sse` expose a network listener (bind host/port below)
  * `"sse"` selects the deprecated standalone HTTP+SSE transport and logs a warning. Use `"http"` for Streamable HTTP in new deployments.
* `CLICKHOUSE_MCP_BIND_HOST`: Host to bind the MCP server to when using HTTP or SSE transport
  * Default: `"127.0.0.1"`
  * Set to `"0.0.0.0"` to bind to all network interfaces (useful for Docker or remote access)
  * Only used when transport is `"http"` or `"sse"` — not related to `CLICKHOUSE_HOST`
* `CLICKHOUSE_MCP_BIND_PORT`: Port to bind the MCP server to when using HTTP or SSE transport
  * Default: `"8000"`
  * Only used when transport is `"http"` or `"sse"` — not related to `CLICKHOUSE_PORT`
* `CLICKHOUSE_MCP_QUERY_TIMEOUT`: Timeout in seconds for query tool calls
  * Default: `"30"`
  * Increase this if you see `Query timed out after ...` errors for heavy queries
  * When a query times out, the server attempts to cancel it with `KILL QUERY`
  * Unless `CLICKHOUSE_SEND_RECEIVE_TIMEOUT` is explicitly set, the HTTP read timeout is capped at this value plus five seconds
* `CLICKHOUSE_MCP_MAX_WORKERS`: Maximum number of concurrent query worker threads
  * Default: `"10"`
  * Increase if your workload requires many concurrent tool calls
  * Metadata tools use a separate pool with `min(4, CLICKHOUSE_MCP_MAX_WORKERS)` threads so schema discovery cannot delay queries
* `CLICKHOUSE_MCP_AUTH_TOKEN`: Static bearer token for HTTP/SSE transports
  * Default: None
  * One of `CLICKHOUSE_MCP_AUTH_TOKEN`, `FASTMCP_SERVER_AUTH`, or `CLICKHOUSE_MCP_AUTH_DISABLED=true` is **required** for HTTP/SSE transports
  * Generate using `uuidgen` or `openssl rand -hex 32`
  * Clients must send this token in the `Authorization: Bearer <token>` header
* `FASTMCP_SERVER_AUTH`: Delegate authentication to a [FastMCP auth provider](https://gofastmcp.com/servers/auth)
  * Default: None
  * Value is the **full class path** of an AuthProvider subclass, e.g. `fastmcp.server.auth.providers.azure.AzureProvider` or `fastmcp.server.auth.providers.google.GoogleProvider`
  * When set, mcp-clickhouse loads the provider from the existing `FASTMCP_SERVER_AUTH_*` environment variables; leave `CLICKHOUSE_MCP_AUTH_TOKEN` unset in this mode
  * Custom providers receive no environment-derived constructor arguments and must support no-argument construction
  * FastMCP 4 no longer supports Supabase HS256 verification. Supabase deployments must use RS256 or ES256.
* `FASTMCP_ENV_FILE`: Optional file containing `FASTMCP_SERVER_AUTH` and provider-specific environment variables
  * Default: None. When unset, the compatibility loader reads missing provider fields from `.env` in the working directory. It does not read `FASTMCP_SERVER_AUTH` from that fallback
  * Set it in the process environment before startup. A value loaded from the default `.env` cannot redirect the compatibility loader
  * If process-set, this file may provide both `FASTMCP_SERVER_AUTH` and provider fields and replaces the working-directory fallback
  * Process environment values take precedence case-insensitively
  * The mcp-clickhouse compatibility loader reads this file only when building HTTP/SSE authentication and reads only `FASTMCP_SERVER_AUTH` and `FASTMCP_SERVER_AUTH_*` entries. FastMCP 4 may read the same file for its broader settings
  * The default `.env` load is separate. It starts at the installed `mcp_clickhouse` package directory, resolves symlinks, walks upward to the filesystem root, and loads the first `.env` found or nothing. It never reads the working directory, regardless of launch method. That file may provide `FASTMCP_SERVER_AUTH` and provider fields along with other server settings. A source checkout normally finds the repository root `.env`
* `CLICKHOUSE_MCP_AUTH_DISABLED`: Disable authentication for HTTP/SSE transports
  * Default: `"false"` (authentication is enabled)
  * Set to `"true"` to disable authentication for local development/testing only
  * **WARNING:** Only use for local development. Do not disable when exposed to networks
* `CLICKHOUSE_MCP_ALLOWED_HOSTS`: Comma separated `Host` header values the HTTP/SSE server answers for
  * Default for a loopback bind: bare and any-port forms of `127.0.0.1`, `localhost`, and `[::1]`
  * If set, the value must contain at least one Host entry.
  * A concrete non-loopback bind address defaults to that address and the configured port. A wildcard bind such as `0.0.0.0` or `::` requires an explicit non-empty value because the public Host cannot be inferred.
  * Host validation is a defense in depth against DNS rebinding. Origin validation below is required separately by MCP.
  * Entries are exact (`localhost:8000`) or accept any port (`localhost:*`). Example: `CLICKHOUSE_MCP_ALLOWED_HOSTS=127.0.0.1:8000,localhost:8000`
  * The `host:*` form matches only values that carry a port. A port-less Host (a standard-port deployment where the client omits `:80`/`:443`) must be listed as a bare exact entry (`example.com`) as well.
  * Requests with a non-matching or missing `Host` header get `421 Misdirected Request`. GET and HEAD requests to `/health` are exempt from Host and Origin validation so orchestrator probes keep working.
  * Behind a reverse proxy, prefer preserving the original `Host` header. You can instead list the upstream `Host` value the proxy sends. Set an explicit list when a launcher such as `fastmcp run` overrides the bind address for remote access.
  * mcp-clickhouse forces FastMCP's separate Host and Origin guard off. `FASTMCP_HTTP_HOST_ORIGIN_PROTECTION`, `FASTMCP_HTTP_ALLOWED_HOSTS`, and `FASTMCP_HTTP_ALLOWED_ORIGINS` do not apply. `CLICKHOUSE_MCP_ALLOWED_HOSTS` and `CLICKHOUSE_MCP_ALLOWED_ORIGINS` are authoritative.
* `CLICKHOUSE_MCP_TRUSTED_PROXIES`: Proxy IP addresses or CIDR networks whose `X-Forwarded-*` headers are trusted
  * Default: None. `X-Forwarded-Host` is ignored. Existing Uvicorn handling of `X-Forwarded-For` and `X-Forwarded-Proto` is unchanged.
  * Entries must be IP addresses or CIDR networks, such as `127.0.0.1,10.20.0.0/24,2001:db8::1`. CIDRs must use their network address, so `10.20.0.1/24` is rejected. Host names, scoped IPv6 addresses, `*`, `0.0.0.0/0`, and `::/0` are also rejected.
  * Trust is based on the immediate raw socket peer. A request from any other peer, or a request without a client address, ignores `X-Forwarded-Host` and validates `Host`.
  * A trusted peer may send exactly one `X-Forwarded-Host` header containing one non-empty value. Duplicate fields, empty values, and comma separated lists get `421 Misdirected Request`. If the header is absent, `Host` is validated.
  * Use the narrowest possible address or network. The MCP server must only be reachable through proxies in the configured ranges. Every trusted proxy must strip and overwrite client supplied `X-Forwarded-Host` and `X-Forwarded-Proto` values, and construct `X-Forwarded-For` from the verified connection peer.
  * The built-in server and `fastmcp run` disable Uvicorn's outer proxy-header handling, validate Host from the raw peer, then apply `X-Forwarded-For` and `X-Forwarded-Proto`. Explicitly enabling `uvicorn_config["proxy_headers"]` fails startup in this mode.
  * Direct ASGI embedding must disable proxy-header handling in the outer ASGI server and call `mcp.http_app(raw_client_address_preserved=True)`. Without that explicit assertion, app construction fails when trusted proxies are configured.
* `CLICKHOUSE_MCP_ALLOWED_ORIGINS`: Comma separated `Origin` header values accepted on HTTP/SSE
  * Default: None, which rejects every request that carries an `Origin` header
  * MCP requires Origin validation for HTTP/SSE transport connections. Requests without an Origin are accepted because non-browser MCP clients normally omit it. A non-matching Origin gets `403 Forbidden`. The `/health` endpoint is exempt as described above.
  * Entries are exact (`http://localhost:3000`) or accept any port (`http://localhost:*`). As with hosts, the any-port form matches only origins that carry a port; a standard-port origin (`https://app.example.com`) must be listed exactly.

##### Reverse proxy Host handling

Preserve `Host` when possible. This keeps forwarded Host trust disabled:

```nginx
location / {
    proxy_pass http://mcp-clickhouse:8000;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Host "";
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Sanitize `X-Forwarded-For` and `X-Forwarded-Proto` independently of `X-Forwarded-Host` trust. Uvicorn may trust those headers based on the proxy peer even when `CLICKHOUSE_MCP_TRUSTED_PROXIES` is unset.

```env
CLICKHOUSE_MCP_ALLOWED_HOSTS=mcp.example.com
```

Stock nginx changes `Host` to the upstream name for proxied requests. It does not create or overwrite `X-Forwarded-Host`. If preserving `Host` is not possible, overwrite the forwarded header at the trusted edge:

```nginx
location / {
    proxy_pass http://mcp-clickhouse:8000;
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

```env
CLICKHOUSE_MCP_ALLOWED_HOSTS=mcp.example.com
CLICKHOUSE_MCP_TRUSTED_PROXIES=10.20.0.8
```

The second configuration is safe only when `10.20.0.8` is the proxy's immediate source address, the server port is isolated from other clients, and nginx overwrites the incoming forwarding headers as shown. For a proxy chain, each trusted hop must discard unverified incoming values before constructing the new forwarding headers.

On an IPv6 or dual-stack bind, IPv4 proxies may appear as IPv4-mapped addresses such as `::ffff:10.20.0.8`; these are matched against IPv4 entries automatically. Envoy's `append_x_forwarded_host` appends to an existing `X-Forwarded-Host` rather than overwriting it, producing a comma separated list that is rejected, so configure the trusted hop to overwrite the header instead. On Kubernetes with source NAT (for example `externalTrafficPolicy: Cluster`) the observed peer may be a node IP rather than the proxy pod, so trust the pod or node CIDR as appropriate; ingress-nginx overwrites both `Host` and `X-Forwarded-Host` itself.

#### Middleware Variables

* `MCP_MIDDLEWARE_MODULE`: Python module name containing custom middleware to inject into the MCP server
  * Default: None (no middleware loaded)
  * Set to the module name (without `.py` extension) of your middleware module
  * The module must provide a `setup_middleware(mcp)` function
  * See [Custom Middleware](#custom-middleware) for details and examples

##### mTLS (Mutual TLS) Variables

These variables enable client certificate authentication for ClickHouse servers that require mutual TLS:

* `CLICKHOUSE_CA_CERT`: Path to CA certificate file
  * Default: None
  * Set this to specify a custom CA certificate for SSL verification
  * Example: `/path/to/ca.crt`
* `CLICKHOUSE_CLIENT_CERT`: Path to client certificate file
  * Default: None
  * Required for mTLS authentication
  * Can be a `.pem` file containing both the certificate and private key
  * Example: `/path/to/client.crt` or `/path/to/client.pem`
* `CLICKHOUSE_CLIENT_CERT_KEY`: Path to client private key file
  * Default: None
  * Optional if `CLICKHOUSE_CLIENT_CERT` is a `.pem` file containing both the certificate and private key
  * Example: `/path/to/client.key`
* `CLICKHOUSE_TLS_MODE`: TLS mode for client certificate authentication
  * Default: None (auto-detected based on `CLICKHOUSE_CLIENT_CERT`)
  * Valid options:
    * `"mutual"` - Use client certificate for authentication (default when `CLICKHOUSE_CLIENT_CERT` is set)
    * `"proxy"` - TLS termination at proxy, use Basic Auth with client certs for TLS only
    * `"strict"` - Strict TLS mode with Basic Auth

#### chDB Variables

* `CHDB_ENABLED`: Enable/disable chDB functionality
  * Default: `"false"`
  * Set to `"true"` to enable chDB tools
  * Requires installing the optional extra: `mcp-clickhouse[chdb]`
* `CHDB_DATA_PATH`: The path to the chDB data directory
  * Default: `":memory:"` (in-memory database)
  * Use `:memory:` for in-memory database
  * Use a file path for persistent storage (e.g., `/path/to/chdb/data`)

#### Common configuration pitfalls

* **`CLICKHOUSE_SECURE` vs MCP / ingress TLS** — Turning off `CLICKHOUSE_SECURE` because the MCP server sits behind Kubernetes ingress, a reverse proxy, or is reached over plain HTTP does not disable database TLS; it only changes how this process connects to ClickHouse. Configure ingress TLS separately from the database client settings.
* **Native protocol ports** — `CLICKHOUSE_PORT` must target ClickHouse's HTTP interface (`8123`/`8443` by default). Ports `9000`/`9440` are for the native TCP protocol (`clickhouse-client`) and will not work with this server.
* **Host confusion** — `CLICKHOUSE_HOST` is the database hostname. `CLICKHOUSE_MCP_BIND_HOST` is only the address the MCP HTTP/SSE server listens on.

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

For ClickHouse with mTLS (Mutual TLS):

```env
# Required variables
CLICKHOUSE_HOST=your-secure-clickhouse.example.com
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=your-user
CLICKHOUSE_PASSWORD=your-password

# mTLS configuration
CLICKHOUSE_SECURE=true
CLICKHOUSE_CA_CERT=/path/to/ca.crt
CLICKHOUSE_CLIENT_CERT=/path/to/client.crt
CLICKHOUSE_CLIENT_CERT_KEY=/path/to/client.key

# Or if using a combined .pem file:
# CLICKHOUSE_CLIENT_CERT=/path/to/client.pem
```

Example Claude Desktop configuration with mTLS:

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
        "CLICKHOUSE_HOST": "your-secure-clickhouse.example.com",
        "CLICKHOUSE_PORT": "8443",
        "CLICKHOUSE_USER": "your-user",
        "CLICKHOUSE_PASSWORD": "your-password",
        "CLICKHOUSE_SECURE": "true",
        "CLICKHOUSE_CA_CERT": "/path/to/ca.crt",
        "CLICKHOUSE_CLIENT_CERT": "/path/to/client.crt",
        "CLICKHOUSE_CLIENT_CERT_KEY": "/path/to/client.key"
      }
    }
  }
}
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
CLICKHOUSE_MCP_AUTH_TOKEN=your-generated-token  # One auth mode required for HTTP/SSE (or FASTMCP_SERVER_AUTH, or CLICKHOUSE_MCP_AUTH_DISABLED=true)
CLICKHOUSE_MCP_ALLOWED_HOSTS=127.0.0.1:4200,localhost:4200,mcp.example.com:4200  # Include every Host value clients and proxies send
```

For local development with HTTP transport (authentication disabled):

```env
CLICKHOUSE_HOST=localhost
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=clickhouse
CLICKHOUSE_MCP_SERVER_TRANSPORT=http
CLICKHOUSE_MCP_AUTH_DISABLED=true  # Only for local development!
CLICKHOUSE_MCP_ALLOWED_HOSTS=127.0.0.1:8000,localhost:8000
```

When using HTTP transport, the server will run on the configured port (default 8000). For example, with the above configuration:
- MCP endpoint: `http://localhost:8000/mcp`
- Health check: `http://localhost:8000/health`

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
        "3.12",
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
