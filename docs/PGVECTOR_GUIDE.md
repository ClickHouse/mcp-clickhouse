# PostgreSQL pgvector MCP 集成指南

本指南介绍如何使用 MCP 服务器中集成的 pgvector 功能。

## 功能特性

### pgvector 工具

* `run_pgvector_select_query`
  * 在 PostgreSQL 数据库上执行 SQL 查询，支持 pgvector 扩展
  * 输入: `query` (字符串): 要执行的 SQL 查询
  * 支持向量相似性搜索和标准 PostgreSQL 查询

* `list_pgvector_tables`
  * 列出 PostgreSQL 数据库中的所有表

* `list_pgvector_vectors`
  * 列出所有向量列及其维度信息
  * 返回每个向量列的表名、列名和维度

* `search_similar_vectors`
  * 执行向量相似性搜索
  * 输入:
    * `table_name` (字符串): 要搜索的表名
    * `vector_column` (字符串): 向量列名
    * `query_vector` (字符串): 查询向量，格式如 '[1,2,3]'
    * `limit` (整数): 返回结果数量，默认 10
    * `distance_function` (字符串): 距离函数 - 'l2', 'cosine', 或 'inner_product'，默认 'l2'

## 配置说明

### 环境变量

#### 必需变量（仅当 PGVECTOR_ENABLED=true 时）

* `PGVECTOR_HOST`: PostgreSQL 服务器主机名
* `PGVECTOR_USER`: 数据库用户名
* `PGVECTOR_PASSWORD`: 数据库密码
* `PGVECTOR_DATABASE`: 数据库名称

#### 可选变量

* `PGVECTOR_ENABLED`: 启用/禁用 pgvector 功能
  * 默认: `"false"`
  * 设置为 `"true"` 启用 pgvector 工具
* `PGVECTOR_PORT`: PostgreSQL 端口号
  * 默认: `"5432"`
* `PGVECTOR_CONNECT_TIMEOUT`: 连接超时时间（秒）
  * 默认: `"30"`
* `PGVECTOR_SSLMODE`: SSL 连接模式
  * 默认: `"prefer"`
  * 有效选项: disable, allow, prefer, require, verify-ca, verify-full

### Claude Desktop 配置示例

在 Claude Desktop 配置文件中添加以下配置：

**仅使用 pgvector:**

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
        "CLICKHOUSE_ENABLED": "false",
        "PGVECTOR_ENABLED": "true",
        "PGVECTOR_HOST": "localhost",
        "PGVECTOR_PORT": "5432",
        "PGVECTOR_USER": "postgres",
        "PGVECTOR_PASSWORD": "your_password",
        "PGVECTOR_DATABASE": "vectordb",
        "PGVECTOR_SSLMODE": "prefer"
      }
    }
  }
}
```

**同时使用 ClickHouse 和 pgvector:**

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
        "PGVECTOR_ENABLED": "true",
        "PGVECTOR_HOST": "localhost",
        "PGVECTOR_PORT": "5432",
        "PGVECTOR_USER": "postgres",
        "PGVECTOR_PASSWORD": "your_password",
        "PGVECTOR_DATABASE": "vectordb"
      }
    }
  }
}
```

### 本地开发配置

1. 在 `test-services/pg-vector` 目录下启动 PostgreSQL 服务:

```bash
cd test-services/pg-vector
docker-compose up -d
```

2. 在项目根目录创建 `.env` 文件并添加以下变量:

```bash
# 禁用 ClickHouse（如果只想测试 pgvector）
CLICKHOUSE_ENABLED=false

# 启用 pgvector
PGVECTOR_ENABLED=true
PGVECTOR_HOST=localhost
PGVECTOR_PORT=5432
PGVECTOR_USER=postgres
PGVECTOR_PASSWORD=postgres
PGVECTOR_DATABASE=vectordb
PGVECTOR_SSLMODE=disable
```

3. 安装依赖:

```bash
uv sync
```

4. 运行 MCP Inspector 进行测试:

```bash
fastmcp dev mcp_server/main.py
```

## 使用示例

### 向量相似性搜索

#### L2 距离（欧几里得距离）
```sql
SELECT id, name, embedding <-> '[1,2,3]' AS distance
FROM items
ORDER BY embedding <-> '[1,2,3]'
LIMIT 10;
```

#### 余弦距离
```sql
SELECT id, name, embedding <=> '[1,2,3]' AS cosine_distance
FROM items
ORDER BY embedding <=> '[1,2,3]'
LIMIT 10;
```

#### 内积（负点积）
```sql
SELECT id, name, embedding <#> '[1,2,3]' AS inner_product
FROM items
ORDER BY embedding <#> '[1,2,3]'
LIMIT 10;
```

### 使用工具函数

```python
# 使用 search_similar_vectors 工具
search_similar_vectors(
    table_name="items",
    vector_column="embedding",
    query_vector="[1,2,3]",
    limit=5,
    distance_function="cosine"
)
```

### 创建向量表

```sql
-- 创建带向量列的表
CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    title TEXT,
    content TEXT,
    embedding vector(1536)  -- 1536维向量（OpenAI embeddings）
);

-- 插入数据
INSERT INTO documents (title, content, embedding) VALUES
    ('文档1', '内容1', '[...]'),  -- 1536维向量
    ('文档2', '内容2', '[...]');

-- 创建 HNSW 索引以提高性能
CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops);
```

### 查询示例

```sql
-- 列出所有表
SELECT * FROM information_schema.tables 
WHERE table_schema = 'public';

-- 查找向量列
SELECT table_name, column_name, 
       substring(data_type from 'vector\((\d+)\)') as dimensions
FROM information_schema.columns
WHERE udt_name = 'vector';

-- 带过滤条件的相似性搜索
SELECT id, title, embedding <=> '[...]' as similarity
FROM documents
WHERE category = 'technology'
ORDER BY embedding <=> '[...]'
LIMIT 10;
```

## 向量索引

pgvector 支持两种索引类型：

### IVFFlat 索引
```sql
-- 适合大型数据集，构建速度快
CREATE INDEX ON items USING ivfflat (embedding vector_l2_ops) WITH (lists = 100);
```

### HNSW 索引（推荐）
```sql
-- 更好的召回率和性能
CREATE INDEX ON items USING hnsw (embedding vector_l2_ops);
CREATE INDEX ON items USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON items USING hnsw (embedding vector_ip_ops);
```

## 距离函数选择

| 距离函数 | 操作符 | 用途 | 适用场景 |
|---------|--------|------|---------|
| L2 距离 | `<->` | 欧几里得距离 | 通用，几何相似性 |
| 余弦距离 | `<=>` | 角度相似性 | 文本嵌入、语义搜索 |
| 内积 | `<#>` | 负点积 | 已归一化的向量 |

## 性能优化建议

1. **创建适当的索引**: 使用 HNSW 索引获得最佳性能
2. **设置工作内存**: 创建索引时增加 `maintenance_work_mem`
3. **使用 LIMIT**: 限制返回结果数量
4. **先过滤后搜索**: 在可能的情况下先应用 WHERE 条件
5. **分析表**: 创建索引后运行 `ANALYZE` 命令

```sql
-- 设置工作内存
SET maintenance_work_mem = '2GB';

-- 创建索引
CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops);

-- 分析表
ANALYZE documents;
```

## 测试数据

测试服务预置了三个示例表：

1. **items**: 3维向量，5条测试数据
2. **documents**: 1536维向量（OpenAI embeddings 维度）
3. **products**: 128维向量，支持所有距离函数

查看预置数据：
```sql
SELECT * FROM items LIMIT 5;
```

## 故障排查

### 连接问题
- 检查 PostgreSQL 服务是否运行: `docker ps`
- 验证连接参数是否正确
- 检查防火墙设置

### pgvector 扩展问题
- 验证扩展已安装: `SELECT * FROM pg_extension WHERE extname = 'vector';`
- 如未安装: `CREATE EXTENSION vector;`

### 性能问题
- 确保已创建适当的索引
- 使用 `EXPLAIN ANALYZE` 分析查询
- 考虑增加 `maintenance_work_mem`

## 更多资源

- [pgvector 官方文档](https://github.com/pgvector/pgvector)
- [PostgreSQL 官方文档](https://www.postgresql.org/docs/)
- [向量搜索最佳实践](https://github.com/pgvector/pgvector#best-practices)

