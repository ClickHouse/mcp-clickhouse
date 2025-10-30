# MyScaleDB Dual-Tool Design Documentation

## Design Overview

The MyScaleDB MCP service now provides two independent SELECT query tools to clearly distinguish between standard queries and similarity searches:

1. **`run_select_query`** - Standard SQL queries
2. **`run_similarity_select_query`** - Vector and hybrid search

## Tool Division

### 1. `run_select_query` - Standard Query Tool

**Purpose**: Execute standard SQL SELECT queries without vector or text search functionality.

**Applicable Scenarios**:
- Data filtering and aggregation
- Table joins (JOIN)
- Statistical analysis
- Data exploration
- Report generation

**Example Queries**:
```sql
-- Data filtering
SELECT * FROM documents WHERE category = 'tech' LIMIT 10;

-- Aggregation analysis
SELECT category, COUNT(*) as count, AVG(price) as avg_price
FROM products
GROUP BY category
ORDER BY count DESC;

-- Table joins
SELECT u.name, o.order_id, o.total
FROM users u
JOIN orders o ON u.id = o.user_id
WHERE o.created_at > '2024-01-01';

-- Statistical analysis
SELECT 
    COUNT(*) as total,
    AVG(value) as avg_value,
    MAX(value) as max_value,
    MIN(value) as min_value
FROM metrics
WHERE timestamp > now() - INTERVAL 1 DAY;
```

**Best Practices**:
- Always use LIMIT to limit result set size
- Use WHERE to filter data before aggregation
- Use appropriate JOIN types
- Use indexed columns for filtering and sorting

**NOT Supported**:
- ❌ `distance()` function
- ❌ `TextSearch()` function
- ❌ `HybridSearch()` function

---

### 2. `run_similarity_select_query` - Similarity Search Tool

**Purpose**: Execute SELECT queries containing vector search, full-text search, or hybrid search.

**Applicable Scenarios**:
- Vector similarity search
- Semantic search
- Full-text search
- Hybrid search (vector + text)
- Recommendation systems
- RAG (Retrieval Augmented Generation) applications

**Supported Functions**:

#### 1. `distance()` - Vector Similarity Search
```sql
-- Basic vector search
SELECT id, text, distance(embedding, [0.1, 0.2, 0.3]) AS dist
FROM documents
ORDER BY dist
LIMIT 10;

-- Vector search with filtering
SELECT id, text, distance(embedding, [0.1, 0.2, 0.3]) AS dist
FROM documents
WHERE category = 'tech'
ORDER BY dist
LIMIT 10;

-- Vector search + threshold filtering
SELECT id, text, distance(embedding, [0.1, 0.2, 0.3]) AS dist
FROM documents
WHERE distance(embedding, [0.1, 0.2, 0.3]) < 0.5
ORDER BY dist
LIMIT 10;
```

**Supported Metric Types**:
- `Cosine` - Cosine similarity (recommended for text embeddings)
- `L2` - Euclidean distance
- `IP` - Inner product

#### 2. `TextSearch()` - Full-Text Search
```sql
-- Basic full-text search
SELECT id, text, TextSearch(text, 'machine learning') AS score
FROM documents
ORDER BY score DESC
LIMIT 10;

-- Full-text search + filtering
SELECT id, text, TextSearch(text, 'AI technology') AS score
FROM documents
WHERE category = 'tech' AND timestamp > '2024-01-01'
ORDER BY score DESC
LIMIT 10;
```

#### 3. `HybridSearch()` - Hybrid Search
```sql
-- Vector + text hybrid search
SELECT 
    id, 
    text,
    HybridSearch(embedding, text, [0.1, 0.2, 0.3], 'machine learning') AS score
FROM documents
ORDER BY score DESC
LIMIT 10;

-- Hybrid search + metadata filtering
SELECT 
    id, 
    text,
    metadata,
    HybridSearch(embedding, text, [0.1, 0.2, 0.3], 'AI') AS score
FROM documents
WHERE JSONExtractString(metadata, 'category') = 'tech'
  AND timestamp > '2024-01-01'
ORDER BY score DESC
LIMIT 10;
```

**Best Practices**:
- Filter by metadata with WHERE first, then perform vector search
- Use appropriate similarity thresholds
- Combine with LIMIT to control result count
- Create appropriate indexes for vector columns (MSTG, SCANN, IVF)
- Create full-text indexes (fts) for text columns and materialize them

---

## Tool Selection Guide

### When to Use `run_select_query`?

✅ Use Cases:
- View table structure and data: `SELECT * FROM table LIMIT 10`
- Data statistics: `SELECT COUNT(*), AVG(col) FROM table`
- Data aggregation: `GROUP BY`, `HAVING`
- Table joins: `JOIN`
- Conditional filtering: `WHERE col = value`
- Sorting: `ORDER BY`
- Time series analysis: `SELECT date, COUNT(*) FROM table GROUP BY date`

❌ Do NOT Use:
- Queries containing `distance()`
- Queries containing `TextSearch()`
- Queries containing `HybridSearch()`

### When to Use `run_similarity_select_query`?

✅ Use Cases:
- Find items most similar to a given vector
- Semantic search: search by text meaning
- Full-text search: keyword search
- Hybrid search: combine semantic and keywords
- Recommendation systems: recommend based on similarity
- Q&A systems: retrieve relevant documents
- RAG applications: retrieve context

❌ Do NOT Use:
- Pure data statistics (without similarity search)
- Pure table joins (without similarity search)
- Simple WHERE filtering (without similarity search)

## Example Comparisons

### Example 1: Data Exploration

**Wrong** ❌:
```sql
-- Don't use run_similarity_select_query for this
SELECT * FROM documents LIMIT 10;
```

**Correct** ✅:
```sql
-- Use run_select_query
SELECT * FROM documents LIMIT 10;
```

### Example 2: Statistical Analysis

**Wrong** ❌:
```sql
-- Don't use run_similarity_select_query for this
SELECT category, COUNT(*) FROM documents GROUP BY category;
```

**Correct** ✅:
```sql
-- Use run_select_query
SELECT category, COUNT(*) FROM documents GROUP BY category;
```

### Example 3: Vector Search

**Wrong** ❌:
```sql
-- Don't use run_select_query for this
SELECT id, distance(embedding, [0.1, 0.2, 0.3]) AS dist
FROM documents
ORDER BY dist LIMIT 10;
```

**Correct** ✅:
```sql
-- Use run_similarity_select_query
SELECT id, distance(embedding, [0.1, 0.2, 0.3]) AS dist
FROM documents
ORDER BY dist LIMIT 10;
```

### Example 4: Hybrid Query (statistics + filtering, but no similarity search)

**Correct** ✅:
```sql
-- Use run_select_query
SELECT category, COUNT(*) as count
FROM documents
WHERE timestamp > '2024-01-01'
GROUP BY category
HAVING count > 10;
```

### Example 5: Vector Search + Statistics

**Correct** ✅:
```sql
-- Use run_similarity_select_query
-- Because it contains distance() function
SELECT 
    category,
    COUNT(*) as count,
    AVG(distance(embedding, [0.1, 0.2, 0.3])) as avg_distance
FROM documents
WHERE distance(embedding, [0.1, 0.2, 0.3]) < 0.5
GROUP BY category;
```

## Implementation Details

### Common Features
- Both tools are set to `readonly=1` to ensure query safety
- Both support timeout control (default 30 seconds)
- Both return structured results (columns + rows)
- Both have error handling and logging

### Differences
- `run_select_query`: Emphasizes standard SQL functionality
- `run_similarity_select_query`: Emphasizes similarity search functionality
- The AI will automatically select the appropriate tool based on query content

## Why Two Tools?

### Advantages

1. **Clear Separation of Responsibilities**:
   - Avoids overly complex tool functionality
   - Each tool focuses on specific types of queries

2. **Better AI Understanding**:
   - The AI can select the correct tool based on query type
   - Tool descriptions are more focused and accurate

3. **Performance Optimization**:
   - Different query types may require different optimization strategies
   - Vector searches may need special indexes and optimizations

4. **Clearer Error Messages**:
   - If the wrong tool is used, clear suggestions can be provided
   - Helps users understand the correct tool usage

### User Experience

Users (or the AI) only need to:
1. Determine if the query includes similarity search functionality
2. Select the appropriate tool
3. Execute the query

The AI will automatically select the appropriate tool based on query content, without user manual specification.

## Summary

This dual-tool design provides:
- ✅ Clear tool responsibilities
- ✅ Better user experience
- ✅ More accurate AI tool selection
- ✅ Focused functionality descriptions
- ✅ Flexible querying capabilities

Remember the key principle:
- 📊 **Standard Queries** → `run_select_query`
- 🔍 **Similarity Search** → `run_similarity_select_query`
