# MCP SQL VectorDB 架构文档

## 项目概述

这是一个基于 FastMCP 框架构建的 MCP 服务器，支持多种数据库和向量搜索引擎：
- **ClickHouse**: 列式数据库
- **chDB**: 嵌入式 ClickHouse 引擎
- **PostgreSQL with pgvector**: 支持向量相似性搜索的关系型数据库

## 项目结构

```
mcp-sqlvectordb/
├── mcp_server/                  # 主要代码目录
│   ├── __init__.py
│   ├── main.py                  # 入口文件
│   ├── mcp_server.py            # MCP 服务器实现和工具注册
│   ├── mcp_env.py               # 环境配置管理
│   ├── chdb_prompt.py           # chDB 提示信息
│   └── pgvector_prompt.py       # pgvector 提示信息
├── test-services/               # 测试服务配置
│   ├── clickhouse/
│   │   └── docker-compose.yaml
│   ├── myscale/
│   │   └── docker-compose.yaml
│   └── pg-vector/
│       ├── docker-compose.yaml  # PostgreSQL + pgvector 服务
│       ├── init.sql             # 初始化脚本
│       └── README.md            # pgvector 服务说明
├── tests/                       # 测试文件
│   ├── test_tool.py             # ClickHouse 工具测试
│   ├── test_chdb_tool.py        # chDB 工具测试
│   ├── test_pgvector_tool.py    # pgvector 工具测试
│   ├── test_mcp_server.py       # MCP 服务器测试
│   └── test_config_interface.py # 配置接口测试
├── pyproject.toml               # 项目配置和依赖
├── README.md                    # 主要文档
├── PGVECTOR_GUIDE.md            # pgvector 使用指南
└── ARCHITECTURE.md              # 架构文档（本文件）
```

## 核心组件

### 1. 配置管理 (mcp_env.py)

负责管理所有数据库连接的环境变量配置，使用单例模式确保配置的全局唯一性。

#### 配置类

- **ClickHouseConfig**: ClickHouse 数据库配置
- **ChDBConfig**: chDB 嵌入式引擎配置
- **PGVectorConfig**: PostgreSQL with pgvector 配置
- **MCPServerConfig**: MCP 服务器级别配置

#### 设计模式

```python
# 单例模式获取配置
config = get_config()           # ClickHouse
chdb_config = get_chdb_config() # chDB
pgvector_config = get_pgvector_config() # pgvector
mcp_config = get_mcp_config()   # MCP Server
```

每个配置类都实现了：
- 环境变量验证
- 默认值处理
- 类型转换
- `get_client_config()` 方法返回客户端连接配置

### 2. MCP 服务器 (mcp_server.py)

核心服务器实现，包含所有数据库工具函数和工具注册逻辑。

#### 架构模式

```
FastMCP 实例
    ├── ClickHouse 工具（可选）
    │   ├── list_databases()
    │   ├── list_tables()
    │   └── run_select_query()
    │
    ├── chDB 工具（可选）
    │   └── run_chdb_select_query()
    │
    └── pgvector 工具（可选）
        ├── run_pgvector_select_query()
        ├── list_pgvector_tables()
        ├── list_pgvector_vectors()
        └── search_similar_vectors()
```

#### 工具注册机制

工具通过环境变量控制是否注册：

```python
# ClickHouse 工具
if os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true":
    mcp.add_tool(Tool.from_function(list_databases))
    mcp.add_tool(Tool.from_function(list_tables))
    mcp.add_tool(Tool.from_function(run_select_query))

# chDB 工具
if os.getenv("CHDB_ENABLED", "false").lower() == "true":
    mcp.add_tool(Tool.from_function(run_chdb_select_query))

# pgvector 工具
if os.getenv("PGVECTOR_ENABLED", "false").lower() == "true":
    mcp.add_tool(Tool.from_function(run_pgvector_select_query))
    mcp.add_tool(Tool.from_function(list_pgvector_tables))
    mcp.add_tool(Tool.from_function(list_pgvector_vectors))
    mcp.add_tool(Tool.from_function(search_similar_vectors))
```

### 3. 入口点 (main.py)

处理不同传输方式的启动逻辑：

- **stdio**: 标准输入/输出（默认）
- **http**: HTTP 传输
- **sse**: Server-Sent Events

```python
def main():
    mcp_config = get_mcp_config()
    transport = mcp_config.server_transport
    
    if transport in [TransportType.HTTP.value, TransportType.SSE.value]:
        mcp.run(transport=transport, host=mcp_config.bind_host, port=mcp_config.bind_port)
    else:
        mcp.run(transport=transport)
```

## 数据流

### ClickHouse 查询流程

```
用户请求
    ↓
run_select_query(query)
    ↓
QUERY_EXECUTOR.submit(execute_query, query)
    ↓
create_clickhouse_client() → clickhouse_connect.get_client()
    ↓
client.query(query, settings={"readonly": read_only})
    ↓
返回结果 {"columns": [...], "rows": [...]}
```

### pgvector 查询流程

```
用户请求
    ↓
run_pgvector_select_query(query) / search_similar_vectors(...)
    ↓
QUERY_EXECUTOR.submit(execute_pgvector_query, query)
    ↓
create_pgvector_client() → psycopg2.connect()
    ↓
cursor.execute(query)
    ↓
返回结果 {"columns": [...], "rows": [...]}
```

### chDB 查询流程

```
用户请求
    ↓
run_chdb_select_query(query)
    ↓
QUERY_EXECUTOR.submit(execute_chdb_query, query)
    ↓
_chdb_client.query(query, "JSON")
    ↓
返回结果 [...]
```

## 并发处理

### ThreadPoolExecutor

使用线程池处理查询，支持超时控制：

```python
QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)

# 提交查询任务
future = QUERY_EXECUTOR.submit(execute_query, query)

# 等待结果，支持超时
timeout_secs = get_mcp_config().query_timeout
result = future.result(timeout=timeout_secs)
```

### 资源清理

使用 `atexit` 确保资源正确释放：

```python
atexit.register(lambda: QUERY_EXECUTOR.shutdown(wait=True))
atexit.register(lambda: _chdb_client.close())
```

## pgvector 特性实现

### 向量相似性搜索

支持三种距离函数：

| 距离函数 | 操作符 | 描述 |
|---------|--------|------|
| L2 距离 | `<->` | 欧几里得距离 |
| 余弦距离 | `<=>` | 角度相似性 |
| 内积 | `<#>` | 负点积 |

### 工具函数

1. **run_pgvector_select_query**: 执行任意 SQL 查询
2. **list_pgvector_tables**: 列出所有用户表
3. **list_pgvector_vectors**: 查找所有向量列及维度
4. **search_similar_vectors**: 执行向量相似性搜索，支持参数化距离函数

### 数据库连接管理

每次查询创建新连接，执行后立即关闭，确保：
- 无连接池状态管理
- 支持并发查询
- 资源及时释放

```python
def execute_pgvector_query(query: str):
    conn = None
    cursor = None
    try:
        conn = create_pgvector_client()
        cursor = conn.cursor()
        cursor.execute(query)
        # ... 处理结果
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
```

## 健康检查

HTTP/SSE 传输模式下提供 `/health` 端点：

```python
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    # 检查 ClickHouse 连接
    if clickhouse_enabled:
        client = create_clickhouse_client()
        version = client.server_version
        return PlainTextResponse(f"OK - Connected to ClickHouse {version}")
    
    # 检查 chDB 状态
    if chdb_enabled:
        return PlainTextResponse("OK - MCP server running with chDB enabled")
```

## 提示系统

每个数据库系统都有专门的提示文件：

- **chdb_prompt.py**: chDB 使用指南和表函数说明
- **pgvector_prompt.py**: pgvector 向量搜索最佳实践

提示通过 FastMCP 的 Prompt 机制注册：

```python
pgvector_prompt = Prompt.from_function(
    pgvector_initial_prompt,
    name="pgvector_initial_prompt",
    description="This prompt helps users understand how to interact with pgvector"
)
mcp.add_prompt(pgvector_prompt)
```

## 环境变量配置

### 优先级

1. 环境变量
2. `.env` 文件（通过 `python-dotenv` 加载）
3. 默认值

### 配置组

- **CLICKHOUSE_***: ClickHouse 相关配置
- **CHDB_***: chDB 相关配置
- **PGVECTOR_***: pgvector 相关配置
- **MCP_***: MCP 服务器配置

## 测试服务

### Docker Compose 服务

每个数据库都有独立的测试服务配置：

1. **ClickHouse**: `test-services/clickhouse/docker-compose.yaml`
2. **MyScale**: `test-services/myscale/docker-compose.yaml`
3. **pgvector**: `test-services/pg-vector/docker-compose.yaml`

### pgvector 测试服务特性

- 基于 `pgvector/pgvector:pg16` 镜像
- 预装 pgvector 扩展
- 包含示例表和数据
- 配置优化的 PostgreSQL 参数
- 健康检查支持

## 依赖管理

使用 `pyproject.toml` 管理依赖：

```toml
[project]
dependencies = [
    "fastmcp>=2.0.0",           # MCP 框架
    "python-dotenv>=1.0.1",      # 环境变量管理
    "clickhouse-connect>=0.8.16", # ClickHouse 客户端
    "truststore>=0.10",          # TLS 证书
    "chdb>=3.3.0",               # chDB 引擎
    "psycopg2-binary>=2.9.9",    # PostgreSQL 客户端
    "pgvector>=0.2.5",           # pgvector Python 库
]
```

## 扩展指南

### 添加新的数据库支持

1. **创建配置类** (`mcp_env.py`):
```python
@dataclass
class NewDBConfig:
    def __init__(self):
        if self.enabled:
            self._validate_required_vars()
    
    @property
    def enabled(self) -> bool:
        return os.getenv("NEWDB_ENABLED", "false").lower() == "true"
    
    # ... 其他配置属性
    
    def get_client_config(self) -> dict:
        return {...}
```

2. **实现工具函数** (`mcp_server.py`):
```python
def create_newdb_client():
    config = get_newdb_config()
    # 创建客户端连接
    return client

def run_newdb_query(query: str):
    # 执行查询
    return result
```

3. **注册工具** (`mcp_server.py`):
```python
if os.getenv("NEWDB_ENABLED", "false").lower() == "true":
    mcp.add_tool(Tool.from_function(run_newdb_query))
```

4. **添加测试** (`tests/test_newdb_tool.py`)

5. **更新文档**

## 最佳实践

### 错误处理

- 使用 `ToolError` 报告工具级错误
- 数据库错误需要回滚事务
- 超时使用 `concurrent.futures.TimeoutError`

### 日志记录

- 使用结构化日志
- 记录连接信息（不含密码）
- 记录查询执行情况和结果数量

### 安全性

- 所有密码通过环境变量配置
- ClickHouse 查询强制 readonly 模式
- 验证必需的环境变量
- 支持 SSL/TLS 连接

### 性能

- 使用线程池处理并发查询
- 支持查询超时
- 及时释放数据库连接
- 使用适当的索引（特别是向量索引）

## 总结

这个项目采用模块化设计，支持多种数据库系统的灵活组合。通过环境变量可以轻松启用或禁用不同的数据库功能。pgvector 的集成提供了强大的向量相似性搜索能力，适合构建 RAG (Retrieval-Augmented Generation) 应用。

