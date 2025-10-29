# pgvector 快速开始指南

这是一个 5 分钟快速开始指南，帮助你快速上手 pgvector 功能。

## 前置要求

- Docker 和 Docker Compose
- Python 3.10+
- uv 或 pip

## 步骤 1: 启动 PostgreSQL + pgvector 服务

```bash
cd test-services/pg-vector
docker-compose up -d
```

验证服务运行：
```bash
docker ps | grep mcp_pgvector_test
```

## 步骤 2: 安装依赖

使用 uv (推荐):
```bash
uv sync
```

或使用 pip:
```bash
pip install -e .
```

## 步骤 3: 配置环境变量

创建 `.env` 文件：

```bash
# 禁用其他服务
CLICKHOUSE_ENABLED=false
CHDB_ENABLED=false

# 启用 pgvector
PGVECTOR_ENABLED=true
PGVECTOR_HOST=localhost
PGVECTOR_PORT=5432
PGVECTOR_USER=postgres
PGVECTOR_PASSWORD=postgres
PGVECTOR_DATABASE=vectordb
PGVECTOR_SSLMODE=disable
```

## 步骤 4: 启动 MCP 服务器

使用 FastMCP Inspector (推荐用于测试):
```bash
source .venv/bin/activate  # 如果使用 uv
fastmcp dev mcp_server/main.py
```

或直接运行:
```bash
python -m mcp_server.main
```

## 步骤 5: 测试功能

### 方式 1: 使用 MCP Inspector (浏览器)

访问 FastMCP Inspector 提供的 URL（通常是 http://localhost:8000）

### 方式 2: 使用 Python

```python
# 测试脚本
import os
os.environ["PGVECTOR_ENABLED"] = "true"
os.environ["PGVECTOR_HOST"] = "localhost"
os.environ["PGVECTOR_PORT"] = "5432"
os.environ["PGVECTOR_USER"] = "postgres"
os.environ["PGVECTOR_PASSWORD"] = "postgres"
os.environ["PGVECTOR_DATABASE"] = "vectordb"

from mcp_server.pgvector import (
    list_pgvector_tables,
    list_pgvector_vectors,
    search_similar_vectors,
    run_pgvector_select_query
)

# 列出所有表
print("Tables:", list_pgvector_tables())

# 列出向量列
print("Vector columns:", list_pgvector_vectors())

# 执行查询
result = run_pgvector_select_query("SELECT * FROM items LIMIT 5")
print("Query result:", result)

# 向量搜索
search_result = search_similar_vectors(
    table_name="items",
    vector_column="embedding",
    query_vector="[1,2,3]",
    limit=5,
    distance_function="l2"
)
print("Search result:", search_result)
```

## 常用操作

### 查看预置数据

```sql
-- 查看 items 表
SELECT * FROM items;

-- 查看表结构
\d items
```

### 向量相似性搜索

```sql
-- L2 距离搜索
SELECT id, name, embedding <-> '[1,2,3]' AS distance
FROM items
ORDER BY embedding <-> '[1,2,3]'
LIMIT 5;

-- 余弦相似度搜索
SELECT id, name, embedding <=> '[1,2,3]' AS cosine_distance
FROM items
ORDER BY embedding <=> '[1,2,3]'
LIMIT 5;
```

### 创建自己的向量表

```sql
-- 创建表
CREATE TABLE my_vectors (
    id SERIAL PRIMARY KEY,
    content TEXT,
    embedding vector(1536)  -- OpenAI embedding 维度
);

-- 插入数据（需要实际的向量）
INSERT INTO my_vectors (content, embedding) VALUES
    ('这是第一条数据', '[0.1, 0.2, ..., 0.3]'),
    ('这是第二条数据', '[0.4, 0.5, ..., 0.6]');

-- 创建索引加速搜索
CREATE INDEX ON my_vectors USING hnsw (embedding vector_cosine_ops);

-- 搜索
SELECT content, embedding <=> '[query_vector]' AS similarity
FROM my_vectors
ORDER BY embedding <=> '[query_vector]'
LIMIT 10;
```

## 与 Claude Desktop 集成

编辑 Claude Desktop 配置文件：
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%/Claude/claude_desktop_config.json`

添加配置：
```json
{
  "mcpServers": {
    "pgvector": {
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
        "PGVECTOR_PASSWORD": "postgres",
        "PGVECTOR_DATABASE": "vectordb",
        "PGVECTOR_SSLMODE": "disable"
      }
    }
  }
}
```

重启 Claude Desktop 即可使用。

## 运行测试

```bash
# 运行所有测试
uv run pytest tests/

# 仅运行 pgvector 测试
uv run pytest tests/test_pgvector_tool.py -v
```

## 清理

停止并删除容器：
```bash
cd test-services/pg-vector
docker-compose down -v
```

## 常见问题

### Q: 连接失败？
A: 检查 Docker 容器是否运行：`docker ps | grep pgvector`

### Q: 向量维度不匹配？
A: 确保查询向量维度与表定义一致

### Q: 搜索很慢？
A: 创建 HNSW 索引：
```sql
CREATE INDEX ON your_table USING hnsw (embedding vector_cosine_ops);
```

### Q: psycopg2 安装失败？
A: 使用二进制版本：`pip install psycopg2-binary`

## 下一步

- 阅读 [PGVECTOR_GUIDE.md](PGVECTOR_GUIDE.md) 了解详细用法
- 阅读 [ARCHITECTURE.md](ARCHITECTURE.md) 了解架构设计
- 查看 [pgvector 官方文档](https://github.com/pgvector/pgvector)

## 获取帮助

- 查看日志：`docker-compose logs -f`
- 检查配置：验证 `.env` 文件和环境变量
- 提交 Issue：在 GitHub 项目页面提交问题

