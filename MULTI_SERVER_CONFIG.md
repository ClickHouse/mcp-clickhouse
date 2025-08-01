# MCP ClickHouse Multi-Server Connection Guide

This document explains how to configure mcp-clickhouse to support multiple ClickHouse server connections and custom MCP server ports.

## Configuring Custom MCP Server Port

You can set the MCP server's listening port and host through environment variables:

```sh
# Default port is 8080, default host is 0.0.0.0
export MCP_SERVER_PORT=9000  # Set custom port
export MCP_SERVER_HOST=127.0.0.1  # Listen only on localhost
```

## Configuring Multiple ClickHouse Servers

You can configure multiple ClickHouse server connections using environment variables.

### Default Connection

The default connection is configured using the following environment variables:

```sh
# Default connection - Required parameters
export CLICKHOUSE_HOST=your-default-clickhouse-host
export CLICKHOUSE_USER=default
export CLICKHOUSE_PASSWORD=your-password

# Default connection - Optional parameters
export CLICKHOUSE_PORT=8443  # Default is 8443 (HTTPS) or 8123 (HTTP)
export CLICKHOUSE_SECURE=true  # Use HTTPS, set to false for HTTP
export CLICKHOUSE_VERIFY=true  # Verify SSL certificates
export CLICKHOUSE_DATABASE=default  # Default database
export CLICKHOUSE_CONNECT_TIMEOUT=30  # Connection timeout (seconds)
export CLICKHOUSE_SEND_RECEIVE_TIMEOUT=300  # Send/receive timeout (seconds)
```

### Additional Connections

To configure additional ClickHouse server connections, first define a list of server names using the `CLICKHOUSE_SERVERS` environment variable:

```sh
# Define additional server names (comma-separated)
export CLICKHOUSE_SERVERS=prod,staging,test
```

Then, configure the corresponding environment variables for each server, using the format `CLICKHOUSE_<SERVER>_<PARAMETER>`:

```sh
# Production ClickHouse server
export CLICKHOUSE_PROD_HOST=prod-clickhouse.example.com
export CLICKHOUSE_PROD_USER=prod_user
export CLICKHOUSE_PROD_PASSWORD=prod_password
export CLICKHOUSE_PROD_DATABASE=analytics

# Test ClickHouse server
export CLICKHOUSE_TEST_HOST=test-clickhouse.example.com
export CLICKHOUSE_TEST_USER=test_user
export CLICKHOUSE_TEST_PASSWORD=test_password
export CLICKHOUSE_TEST_PORT=9440
```

## Using Multi-Server Connections

In the MCP ClickHouse tools, you can specify which server to use with the additional `clickhouse_server` parameter:

```python
# Query databases on a specific server
list_databases(clickhouse_server="prod")

# Query tables on a specific server
list_tables(database="analytics", clickhouse_server="prod")

# Execute a query on a specific server
run_select_query("SELECT count() FROM analytics.events", clickhouse_server="prod")
```

If you don't specify the `clickhouse_server` parameter, the default server (configured by the `CLICKHOUSE_HOST` etc. environment variables) will be used.

## Viewing Available Servers

You can use the `list_clickhouse_servers()` function to list all configured ClickHouse servers:

```python
servers = list_clickhouse_servers()
print(servers)  # ['default', 'prod', 'staging', 'test']
```

## Adding this Server to the MCP Client

If you want to add this server to an MCP client, make sure you have set a custom port and use the following command to add it to the MCP client:

```sh
mcp add-server <server-name> http://localhost:<MCP_SERVER_PORT>
```

For example:

```sh
mcp add-server clickhouse-analyzer http://localhost:9000
```

After adding it, you can interact with the server in the MCP client, including selecting different ClickHouse servers for queries. 