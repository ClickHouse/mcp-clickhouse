# MCP SQL VectorDB Architecture Documentation

## Project Overview

This is an MCP server built on the FastMCP framework, supporting multiple databases and vector search engines:
- **MyScale**: Columnar database
- **chDB**: Embedded MyScale engine
- **PostgreSQL with pgvector**: Relational database with vector similarity search support

## Project Structure

```
mcp-sqlvectordb/
├── mcp_server/                  # Main code directory
│   ├── __init__.py
│   ├── main.py                  # Entry point
│   ├── mcp_server.py            # MCP server implementation and tool registration
│   ├── mcp_env.py               # Environment configuration management
│   ├── chdb_prompt.py           # chDB prompt information
│   └── pgvector_prompt.py       # pgvector prompt information
├── test-services/               # Test service configuration
│   ├── MyScale/
│   │   └── docker-compose.yaml
│   ├── myscale/
│   │   └── docker-compose.yaml
│   └── pg-vector/
│       ├── docker-compose.yaml  # PostgreSQL + pgvector service
│       ├── init.sql             # Initialization script
│       └── README.md            # pgvector service documentation
├── tests/                       # Test files
│   ├── test_tool.py             # MyScale tool tests
│   ├── test_chdb_tool.py        # chDB tool tests
│   ├── test_pgvector_tool.py    # pgvector tool tests
│   ├── test_mcp_server.py       # MCP server tests
│   └── test_config_interface.py # Configuration interface tests
├── pyproject.toml               # Project configuration and dependencies
├── README.md                    # Main documentation
├── PGVECTOR_GUIDE.md            # pgvector usage guide
└── ARCHITECTURE.md              # Architecture documentation (this file)
```

## Core Components

### 1. Configuration Management (mcp_env.py)

Manages environment variable configuration for all database connections, using singleton pattern to ensure global uniqueness.

#### Configuration Classes

- **MyScaleConfig**: MyScale database configuration
- **ChDBConfig**: chDB embedded engine configuration
- **PGVectorConfig**: PostgreSQL with pgvector configuration
- **MCPServerConfig**: MCP server-level configuration

#### Design Pattern

```python
# Singleton pattern for configuration access
config = get_config()           # MyScale
chdb_config = get_chdb_config() # chDB
pgvector_config = get_pgvector_config() # pgvector
mcp_config = get_mcp_config()   # MCP Server
```

Each configuration class implements:
- Environment variable validation
- Default value handling
- Type conversion
- `get_client_config()` method returning client connection configuration

### 2. MCP Server (mcp_server.py)

Core server implementation containing all database tool functions and tool registration logic.

#### Architecture Pattern

```
FastMCP Instance
    ├── MyScale Tools (optional)
    │   ├── list_databases()
    │   ├── list_tables()
    │   ├── run_similarity_select_query()
    │   └── run_select_query()
    │
    ├── chDB Tools (optional)
    │   └── run_chdb_select_query()
    │
    └── pgvector Tools (optional)
        ├── run_pgvector_select_query()
        ├── list_pgvector_tables()
        ├── list_pgvector_vectors()
        └── search_similar_vectors()
```

#### Tool Registration Mechanism

Tools are registered based on environment variables:

```python
# MyScale tools
if os.getenv("MYSCALE_ENABLED", "true").lower() == "true":
    mcp.add_tool(Tool.from_function(list_databases))
    mcp.add_tool(Tool.from_function(list_tables))
    mcp.add_tool(Tool.from_function(run_similarity_select_query))
    mcp.add_tool(Tool.from_function(run_select_query))

# chDB tools
if os.getenv("CHDB_ENABLED", "false").lower() == "true":
    mcp.add_tool(Tool.from_function(run_chdb_select_query))

# pgvector tools
if os.getenv("PGVECTOR_ENABLED", "false").lower() == "true":
    mcp.add_tool(Tool.from_function(run_pgvector_select_query))
    mcp.add_tool(Tool.from_function(list_pgvector_tables))
    mcp.add_tool(Tool.from_function(list_pgvector_vectors))
    mcp.add_tool(Tool.from_function(search_similar_vectors))
```

### 3. Entry Point (main.py)

Handles startup logic for different transport modes:

- **stdio**: Standard input/output (default)
- **http**: HTTP transport
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

## Data Flow

### MyScale Query Flow

```
User Request
    ↓
run_select_query(query)
    ↓
QUERY_EXECUTOR.submit(execute_query, query)
    ↓
create_myscale_client() → clickhouse_connect.get_client()
    ↓
client.query(query, settings={"readonly": read_only})
    ↓
Return results {"columns": [...], "rows": [...]}
```

### pgvector Query Flow

```
User Request
    ↓
run_pgvector_select_query(query) / search_similar_vectors(...)
    ↓
QUERY_EXECUTOR.submit(execute_pgvector_query, query)
    ↓
create_pgvector_client() → psycopg2.connect()
    ↓
cursor.execute(query)
    ↓
Return results {"columns": [...], "rows": [...]}
```

### chDB Query Flow

```
User Request
    ↓
run_chdb_select_query(query)
    ↓
QUERY_EXECUTOR.submit(execute_chdb_query, query)
    ↓
_chdb_client.query(query, "JSON")
    ↓
Return results [...]
```

## Concurrency Handling

### ThreadPoolExecutor

Uses thread pool to handle queries with timeout support:

```python
QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)

# Submit query task
future = QUERY_EXECUTOR.submit(execute_query, query)

# Wait for result with timeout support
timeout_secs = get_mcp_config().query_timeout
result = future.result(timeout=timeout_secs)
```

### Resource Cleanup

Uses `atexit` to ensure proper resource release:

```python
atexit.register(lambda: QUERY_EXECUTOR.shutdown(wait=True))
atexit.register(lambda: _chdb_client.close())
```

## pgvector Feature Implementation

### Vector Similarity Search

Supports three distance functions:

| Distance Function | Operator | Description |
|------------------|----------|-------------|
| L2 Distance | `<->` | Euclidean distance |
| Cosine Distance | `<=>` | Angular similarity |
| Inner Product | `<#>` | Negative dot product |

### Tool Functions

1. **run_pgvector_select_query**: Execute arbitrary SQL queries
2. **list_pgvector_tables**: List all user tables
3. **list_pgvector_vectors**: Find all vector columns and their dimensions
4. **search_similar_vectors**: Execute vector similarity search with parameterized distance functions

### Database Connection Management

Creates new connection for each query and closes immediately after execution, ensuring:
- No connection pool state management
- Support for concurrent queries
- Timely resource release

```python
def execute_pgvector_query(query: str):
    conn = None
    cursor = None
    try:
        conn = create_pgvector_client()
        cursor = conn.cursor()
        cursor.execute(query)
        # ... process results
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
```

## Health Check

Provides `/health` endpoint for HTTP/SSE transport modes:

```python
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    # Check MyScale connection
    if myscale_enabled:
        client = create_myscale_client()
        version = client.server_version
        return PlainTextResponse(f"OK - Connected to MyScale {version}")
    
    # Check chDB status
    if chdb_enabled:
        return PlainTextResponse("OK - MCP server running with chDB enabled")
```

## Prompt System

Each database system has dedicated prompt files:

- **chdb_prompt.py**: chDB usage guide and table function documentation
- **pgvector_prompt.py**: pgvector vector search best practices

Prompts are registered through FastMCP's Prompt mechanism:

```python
pgvector_prompt = Prompt.from_function(
    pgvector_initial_prompt,
    name="pgvector_initial_prompt",
    description="This prompt helps users understand how to interact with pgvector"
)
mcp.add_prompt(pgvector_prompt)
```

## Environment Variable Configuration

### Priority

1. Environment variables
2. `.env` file (loaded via `python-dotenv`)
3. Default values

### Configuration Groups

- **MYSCALE_***: MyScale related configuration
- **CHDB_***: chDB related configuration
- **PGVECTOR_***: pgvector related configuration
- **MCP_***: MCP server configuration

## Test Services

### Docker Compose Services

Each database has independent test service configuration:

1. **MyScale**: `test-services/MyScale/docker-compose.yaml`
2. **myscale**: `test-services/myscale/docker-compose.yaml`
3. **pgvector**: `test-services/pg-vector/docker-compose.yaml`

### pgvector Test Service Features

- Based on `pgvector/pgvector:pg16` image
- Pre-installed pgvector extension
- Includes sample tables and data
- Optimized PostgreSQL parameters
- Health check support

## Dependency Management

Uses `pyproject.toml` to manage dependencies:

```toml
[project]
dependencies = [
    "fastmcp>=2.0.0",           # MCP framework
    "python-dotenv>=1.0.1",      # Environment variable management
    "clickhouse-connect>=0.8.16", # ClickHouse client
    "truststore>=0.10",          # TLS certificates
    "chdb>=3.3.0",               # chDB engine
    "psycopg2-binary>=2.9.9",    # PostgreSQL client
    "pgvector>=0.2.5",           # pgvector Python library
]
```

## Extension Guide

### Adding New Database Support

1. **Create Configuration Class** (`mcp_env.py`):
```python
@dataclass
class NewDBConfig:
    def __init__(self):
        if self.enabled:
            self._validate_required_vars()
    
    @property
    def enabled(self) -> bool:
        return os.getenv("NEWDB_ENABLED", "false").lower() == "true"
    
    # ... other configuration properties
    
    def get_client_config(self) -> dict:
        return {...}
```

2. **Implement Tool Functions** (`mcp_server.py`):
```python
def create_newdb_client():
    config = get_newdb_config()
    # Create client connection
    return client

def run_newdb_query(query: str):
    # Execute query
    return result
```

3. **Register Tools** (`mcp_server.py`):
```python
if os.getenv("NEWDB_ENABLED", "false").lower() == "true":
    mcp.add_tool(Tool.from_function(run_newdb_query))
```

4. **Add Tests** (`tests/test_newdb_tool.py`)

5. **Update Documentation**

## Best Practices

### Error Handling

- Use `ToolError` to report tool-level errors
- Database errors require transaction rollback
- Use `concurrent.futures.TimeoutError` for timeouts

### Logging

- Use structured logging
- Log connection information (without passwords)
- Log query execution and result counts

### Security

- All passwords configured via environment variables
- MyScale queries enforce readonly mode
- Validate required environment variables
- Support SSL/TLS connections

### Performance

- Use thread pool for concurrent query handling
- Support query timeouts
- Release database connections promptly
- Use appropriate indexes (especially vector indexes)

## Summary

This project adopts a modular design supporting flexible combinations of multiple database systems. Different database functionalities can be easily enabled or disabled through environment variables. The pgvector integration provides powerful vector similarity search capabilities, suitable for building RAG (Retrieval-Augmented Generation) applications.
