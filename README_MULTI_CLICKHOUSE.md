# MCP ClickHouse 多服务器连接指南

本文档说明如何配置 mcp-clickhouse 以支持多个 ClickHouse 服务器连接和自定义 MCP 服务器端口。

## 配置自定义 MCP 服务器端口

您可以通过环境变量设置 MCP 服务器的监听端口和主机：

```sh
# 默认端口为 8080，默认主机为 0.0.0.0
export MCP_SERVER_PORT=9000  # 设置自定义端口
export MCP_SERVER_HOST=127.0.0.1  # 仅监听本地连接
```

## 配置多个 ClickHouse 服务器

您可以通过环境变量配置多个 ClickHouse 服务器连接。

### 默认连接

默认连接使用以下环境变量配置：

```sh
# 默认连接 - 必需参数
export CLICKHOUSE_HOST=your-default-clickhouse-host
export CLICKHOUSE_USER=default
export CLICKHOUSE_PASSWORD=your-password

# 默认连接 - 可选参数
export CLICKHOUSE_PORT=8443  # 默认为 8443 (HTTPS) 或 8123 (HTTP)
export CLICKHOUSE_SECURE=true  # 使用 HTTPS，设为 false 则使用 HTTP
export CLICKHOUSE_VERIFY=true  # 验证 SSL 证书
export CLICKHOUSE_DATABASE=default  # 默认数据库
export CLICKHOUSE_CONNECT_TIMEOUT=30  # 连接超时时间（秒）
export CLICKHOUSE_SEND_RECEIVE_TIMEOUT=300  # 发送/接收超时时间（秒）
```

### 额外连接

要配置额外的 ClickHouse 服务器连接，首先通过 `CLICKHOUSE_SERVERS` 环境变量定义服务器名称列表：

```sh
# 定义额外的服务器名称（逗号分隔）
export CLICKHOUSE_SERVERS=prod,staging,test
```

然后，为每个服务器配置相应的环境变量，格式为 `CLICKHOUSE_<SERVER>_<PARAMETER>`：

```sh
# 生产环境 ClickHouse 服务器
export CLICKHOUSE_PROD_HOST=prod-clickhouse.example.com
export CLICKHOUSE_PROD_USER=prod_user
export CLICKHOUSE_PROD_PASSWORD=prod_password
export CLICKHOUSE_PROD_DATABASE=analytics

# 测试环境 ClickHouse 服务器
export CLICKHOUSE_TEST_HOST=test-clickhouse.example.com
export CLICKHOUSE_TEST_USER=test_user
export CLICKHOUSE_TEST_PASSWORD=test_password
export CLICKHOUSE_TEST_PORT=9440
```

## 使用多服务器连接

在 MCP ClickHouse 工具中，您可以通过额外的 `clickhouse_server` 参数指定要使用的服务器：

```python
# 查询特定服务器上的数据库
list_databases(clickhouse_server="prod")

# 查询特定服务器上的表
list_tables(database="analytics", clickhouse_server="prod")

# 在特定服务器上执行查询
run_select_query("SELECT count() FROM analytics.events", clickhouse_server="prod")
```

如果不指定 `clickhouse_server` 参数，则使用默认服务器（由 `CLICKHOUSE_HOST` 等环境变量配置）。

## 查看可用服务器

您可以使用 `list_clickhouse_servers()` 函数列出所有配置的 ClickHouse 服务器：

```python
servers = list_clickhouse_servers()
print(servers)  # ['default', 'prod', 'staging', 'test']
```

## 在 MCP 客户端中添加此服务器

如果您想在 MCP 客户端中添加此服务器，请确保设置了自定义端口，并使用以下命令将其添加到 MCP 客户端：

```sh
mcp add-server <server-name> http://localhost:<MCP_SERVER_PORT>
```

例如：

```sh
mcp add-server clickhouse-analyzer http://localhost:9000
```

添加后，您可以在 MCP 客户端中与服务器交互，包括选择不同的 ClickHouse 服务器进行查询。 