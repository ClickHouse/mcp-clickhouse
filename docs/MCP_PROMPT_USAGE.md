# MCP Prompt Usage Guide

## Problem Description

In MCP (Model Context Protocol), **prompts do not automatically become system prompts**. They are resources that can be called by clients and need to be explicitly loaded and sent to the AI by the client.

### Why doesn't the AI understand the rules in prompts.py?

When you define detailed rules in `prompts.py` (such as vector index creation, full-text index creation, etc.) and register them as follows:

```python
myscaledb_prompt = Prompt.from_function(
    myscaledb_initial_prompt,
    name="myscaledb_initial_prompt",
    description="This prompt helps users understand how to interact with MyScaleDB and perform operations.",
)
mcp.add_prompt(myscaledb_prompt)
```

These prompts **are not automatically loaded** to the AI. They are only registered as MCP resources, and the AI cannot see these rules unless the client (such as Claude Desktop) explicitly calls these prompts.

## Solution

### Method 1: Include Key Rules in Tool Docstrings (Implemented)

We have directly added key rules to the tool docstrings. This way, when the AI sees the tool list, it can immediately understand these constraints and best practices.

#### MyScaleDB Example

```python
def run_select_query(query: str):
    """Run a SELECT query in a MyScaleDB database.
    
    MyScaleDB = ClickHouse + Vector Search
    
    Available Query Functions:
    1. Vector Search:
       - distance(embedding, [0.1, 0.2, 0.3]): Calculate vector similarity
       - Supported metrics: Cosine, L2, IP (Inner Product)
       - Example: SELECT id, distance(embedding, [0.1, 0.2, 0.3]) AS dist FROM tbl ORDER BY dist LIMIT 10
    
    2. Full Text Search:
       - TextSearch(text_col, 'search query'): Full text search with score
       - Example: SELECT id, TextSearch(text, 'machine learning') AS score FROM tbl ORDER BY score DESC LIMIT 10
    
    3. Hybrid Search:
       - HybridSearch(embedding, text_col, [0.1, 0.2, 0.3], 'search query'): Combine vector and text search
       - Example: SELECT id, HybridSearch(embedding, text, [0.1, 0.2, 0.3], 'AI') AS score FROM tbl ORDER BY score DESC LIMIT 10
    
    Best Practices:
    - Always use LIMIT to prevent large result sets
    - Combine filters with vector/text search for better results
    - Filter first, then search: WHERE category='tech' AND distance(...) < 0.5
    """
```

**Note**: Tool docstrings only include query-related functionality (distance, TextSearch, HybridSearch), not index creation rules, because `run_select_query` can only execute SELECT queries (readonly=1).

#### pgvector Example

```python
def run_pgvector_select_query(query: str):
    """Run a SELECT query on PostgreSQL with pgvector support.
    
    PostgreSQL + pgvector extension
    
    Available Query Functions:
    1. Vector Similarity Search:
       - <-> : L2 distance (Euclidean) - for general vector comparison
       - <=> : Cosine distance - for text embeddings (recommended)
       - <#> : Inner product distance - for normalized vectors
       - Example: SELECT id, vec_col <=> '[0.1,0.2,0.3]' AS distance FROM tbl ORDER BY distance LIMIT 10
    
    2. Full Text Search:
       - to_tsvector('english', text_col): Convert text to search vector
       - to_tsquery('search & terms'): Create search query
       - @@ operator: Match operation
       - Example: SELECT id FROM tbl WHERE to_tsvector('english', text) @@ to_tsquery('machine & learning') LIMIT 10
    
    3. Hybrid Queries:
       - Combine vector search with full-text search
       - Example: SELECT id FROM tbl WHERE to_tsvector('english', text) @@ to_tsquery('AI') ORDER BY vec_col <=> '[...]' LIMIT 10
    
    Best Practices:
    - Use LIMIT to prevent large result sets
    - Filter with WHERE first, then perform vector search
    - Combine multiple search conditions for better results
    """
```

**Note**: Similarly, only includes query operators and functions, not index creation rules.

#### chDB Example

```python
def run_chdb_select_query(query: str):
    """Run SQL in chDB, an in-process ClickHouse engine.
    
    chDB = In-Process ClickHouse + Direct Query via Table Functions
    
    Key Features:
    1. Table Functions - Query data sources directly without import:
       - Local files: file('path/to/file.csv') or file('data.parquet', 'Parquet')
       - Remote files: url('https://example.com/data.csv', 'CSV')
       - S3 storage: s3('s3://bucket/path/file.csv', 'CSV')
       - PostgreSQL: postgresql('host:port', 'database', 'table', 'user', 'password')
       - MySQL: mysql('host:port', 'database', 'table', 'user', 'password')
    
    2. Supported Formats:
       - CSV, TSV, JSON, JSONEachRow
       - Parquet, ORC, Avro
    
    3. Best Practices:
       - Use LIMIT to prevent large result sets (recommend LIMIT 10 by default)
       - Use WHERE to filter and reduce data transfer
       - Use SELECT to specify columns and avoid full table scans
       - Multi-source JOIN: file() JOIN url() JOIN s3()
       - Test connection: DESCRIBE table_function(...)
    
    4. No Data Import Required:
       - Query data in place
       - If no suitable table function exists, use Python to download to temp file and query with file()
    """
```

### Method 2: Add Custom Instructions in Client Configuration

If you use Claude Desktop, you can add custom instructions in the configuration file, but this requires manual maintenance and is less convenient than Method 1.

### Method 3: Create an Initialization Tool

You can create a dedicated tool that returns usage guidelines, but this requires users to call the tool at the beginning of each session.

## Best Practices

1. **Put key rules in tool docstrings**: Ensure the AI understands constraints when it sees the tool list
2. **Keep prompts.py as reference documentation**: It's still a good place for detailed documentation
3. **Concise but complete**: Provide enough information in docstrings without being too verbose
4. **Include examples**: Specific SQL examples are more helpful than abstract descriptions

## Verification Method

Test if the AI understands these rules:

1. Ask the AI: "How do I create a vector index in MyScaleDB?"
2. Check if the AI's answer includes the correct syntax (like `ALTER TABLE ... ADD VECTOR INDEX ... TYPE MSTG`)
3. Ask about full-text indexes and verify if the AI knows about materialization

If the AI can answer these questions correctly, it means the information in tool docstrings is being properly conveyed.

## Modified Files

- `/home/shanfengp/originhub/mcp-sqlvectordb/mcp_server/myscaledb/server.py`
  - Updated `run_select_query` docstring - added query function descriptions (distance, TextSearch, HybridSearch)
  - Updated `list_tables` docstring - added vector column identification description

- `/home/shanfengp/originhub/mcp-sqlvectordb/mcp_server/pgvector/server.py`
  - Updated `run_pgvector_select_query` docstring - added query operator descriptions (<->, <=>, <#>) and full-text search functions
  - Updated `list_pgvector_vectors` docstring - added detailed vector column description
  - Updated `search_similar_vectors` docstring - added detailed distance function description

- `/home/shanfengp/originhub/mcp-sqlvectordb/mcp_server/chdb/server.py`
  - Updated `run_chdb_select_query` docstring - added table function usage guide and best practices

**Important Principle**: SELECT query tool docstrings only include query-related functionality (functions, operators), not index creation rules, because these tools can only execute SELECT statements (readonly=1).

## Summary

MCP prompts are a great feature, but they are not automatically applied. By including key rules in tool docstrings, we ensure the AI can see this important information every time it uses the tools.

For SELECT query tools, we only include query-related functionality:
- **MyScaleDB**: distance(), TextSearch(), HybridSearch() functions
- **pgvector**: Vector distance operators (<->, <=>, <#>) and full-text search functions (to_tsvector, to_tsquery)
- **chDB**: Table functions (file, url, s3, postgresql, mysql)

This allows the AI to correctly understand and use these query functions without attempting to execute index creation or other unsupported operations through readonly SELECT tools.
