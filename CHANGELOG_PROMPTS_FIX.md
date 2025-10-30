# MCP Prompts Issue Fix Record

## Problem Description

Users reported that the MCP service could not understand the text index and vector index creation rules defined in `prompts.py`.

## Root Cause

In the MCP (Model Context Protocol), **prompts do not automatically become system prompts**. They are resources that can be explicitly called by clients, and are not automatically loaded to the AI.

### Technical Details

When registering a prompt as follows:

```python
mcp.add_prompt(Prompt.from_function(
    myscaledb_initial_prompt,
    name="myscaledb_initial_prompt",
    description="...",
))
```

This prompt is only registered as an MCP resource. Unless the client (such as Claude Desktop) explicitly calls it, the AI cannot see its content.

## Solution

### Implementation Method

Embed key rules and best practices directly into the tool's docstring. This way, when the AI sees the tool list (which happens automatically), it can immediately understand these constraints and rules.

### Specific Modifications

#### 1. MyScaleDB Service (`mcp_server/myscaledb/server.py`)

**`run_select_query` Tool**:
- ✅ Added query function descriptions: distance(), TextSearch(), HybridSearch()
- ✅ Added function usage examples and supported metric types
- ✅ Added query best practices (LIMIT, filter first then search, etc.)
- ❌ **Does NOT include index creation rules** (because it's a readonly SELECT tool)

**`list_tables` Tool**:
- ✅ Explained how to identify vector columns (Array(Float32) type)
- ✅ Emphasized identifying vector columns before creating indexes

#### 2. pgvector Service (`mcp_server/pgvector/server.py`)

**`run_pgvector_select_query` Tool**:
- ✅ Added vector distance operator descriptions (<->, <=>, <#>) and their use cases
- ✅ Added full-text search functions (to_tsvector, to_tsquery, @@ operator)
- ✅ Added hybrid query examples
- ✅ Added query best practices
- ❌ **Does NOT include index creation rules** (because it's a SELECT query tool)

**`list_pgvector_vectors` Tool**:
- ✅ Described the format of returned vector column information
- ✅ Emphasized the importance of dimension identification

**`search_similar_vectors` Tool**:
- ✅ Detailed descriptions of three distance function use cases
- ✅ Reminded that indexes need to be created for optimal performance

#### 3. chDB Service (`mcp_server/chdb/server.py`)

**`run_chdb_select_query` Tool**:
- ✅ Added table function usage guide (file, url, s3, postgresql, mysql)
- ✅ Added list of supported formats
- ✅ Emphasized the no-import-needed feature
- ✅ Added best practices (LIMIT, WHERE, SELECT optimization, etc.)

### Documentation Updates

Detailed documentation created:
- ✅ `docs/MCP_PROMPT_USAGE.md` - Complete problem analysis and solution description
- ✅ `CHANGELOG_PROMPTS_FIX.md` - This fix record

## Verification of Results

### Test Methods

1. **MyScaleDB Vector Search Test**:
   ```
   Question: How to perform vector similarity search in MyScaleDB?
   Expected: AI response includes distance(embedding, [...]) function and ORDER BY example
   ```

2. **MyScaleDB Hybrid Search Test**:
   ```
   Question: How to perform hybrid search in MyScaleDB?
   Expected: AI response includes HybridSearch() or TextSearch() function
   ```

3. **pgvector Vector Search Test**:
   ```
   Question: How to perform cosine similarity search in pgvector?
   Expected: AI response includes <=> operator and correct query syntax
   ```

4. **chDB Table Function Test**:
   ```
   Question: How to query CSV files in chDB?
   Expected: AI response includes file('path/to/file.csv') table function
   ```

### Expected Results

- ✅ AI can correctly use vector search functions (distance, HybridSearch, TextSearch)
- ✅ AI can correctly use pgvector's distance operators (<->, <=>, <#>)
- ✅ AI can correctly use full-text search functions (to_tsvector, to_tsquery)
- ✅ AI understands query best practices (LIMIT, filter first then search)
- ✅ AI understands table function usage (file, url, s3)
- ✅ AI will not attempt to execute index creation through readonly SELECT tools

## Technical Points

### Why Does This Approach Work?

1. **Tool descriptions are automatically loaded**: The AI automatically sees all docstrings when fetching the available tool list
2. **Immediate visibility**: No need for clients to call any additional resources
3. **Context-relevant**: Rules appear directly in the relevant tool descriptions
4. **Easy to maintain**: Code and documentation are in the same place

### Relationship with prompts.py

- `prompts.py` is still retained as detailed reference documentation
- Tool docstrings contain streamlined key rules
- If clients support explicit prompt calls, complete detailed information can still be obtained

## Related Resources

- **MCP Protocol Documentation**: https://modelcontextprotocol.io/
- **FastMCP Documentation**: https://github.com/jlowin/fastmcp
- **MyScaleDB Documentation**: https://myscale.com/docs/
- **pgvector Documentation**: https://github.com/pgvector/pgvector

## Summary

By migrating key rules from `prompts.py` to tool docstrings, we solved the problem of the AI not understanding query functions and operators. This is a simple but effective solution that leverages the automatic loading feature of tool descriptions in the MCP protocol.

**Important Design Decision**: SELECT query tool docstrings only include query-related functionality, not index creation rules, because:
1. These tools are set to `readonly=1` and can only execute SELECT queries
2. Avoids misleading the AI to attempt unsupported operations through these tools
3. Maintains focus and accuracy of tool descriptions

Users can now expect the AI to correctly understand and apply:
- MyScaleDB's query functions (distance, TextSearch, HybridSearch)
- pgvector's distance operators (<->, <=>, <#>) and full-text search functions
- chDB's table functions (file, url, s3, postgresql, mysql)
- Query best practices (LIMIT, filter first then search)

---

**Fix Date**: 2025-10-30  
**Fixed By**: AI Assistant  
**Scope of Impact**: All three services (MyScaleDB, pgvector, chDB)
