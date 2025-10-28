# PostgreSQL with pgvector Test Service

这个目录包含用于测试 pgvector 功能的 PostgreSQL 服务配置。

## 启动服务

```bash
cd test-services/pg-vector
docker-compose up -d
```

## 停止服务

```bash
docker-compose down
```

## 清理数据

```bash
docker-compose down -v
```

## 连接信息

- **Host**: localhost
- **Port**: 5432
- **Database**: vectordb
- **User**: postgres
- **Password**: postgres

## 环境变量配置

在项目根目录的 `.env` 文件中添加以下配置：

```bash
PGVECTOR_ENABLED=true
PGVECTOR_HOST=localhost
PGVECTOR_PORT=5432
PGVECTOR_USER=postgres
PGVECTOR_PASSWORD=postgres
PGVECTOR_DATABASE=vectordb
PGVECTOR_SSLMODE=disable
```

## 预置的测试表

### 1. items 表
- 3维向量示例
- 包含 5 条测试数据
- 使用 HNSW 索引 (L2 距离)

### 2. documents 表
- 1536维向量（OpenAI embeddings 常用维度）
- 支持 JSONB 元数据
- 使用 HNSW 索引 (Cosine 距离)

### 3. products 表
- 128维向量
- 包含价格和分类信息
- 支持三种距离函数索引（L2, Cosine, Inner Product）

## 测试查询示例

```sql
-- 查看所有表
SELECT * FROM information_schema.tables 
WHERE table_schema = 'public';

-- 查询向量列信息
SELECT * FROM information_schema.columns 
WHERE udt_name = 'vector';

-- 执行相似性搜索
SELECT name, embedding <-> '[1,2,3]' as distance
FROM items
ORDER BY embedding <-> '[1,2,3]'
LIMIT 5;
```

## 验证 pgvector 安装

连接到数据库后运行：

```sql
SELECT * FROM pg_extension WHERE extname = 'vector';
```

