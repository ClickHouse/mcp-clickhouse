"""Tests for pgvector tools."""

import os
import sys
import pytest
from unittest.mock import Mock, patch, MagicMock


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Set up environment variables for testing."""
    monkeypatch.setenv("PGVECTOR_ENABLED", "true")
    monkeypatch.setenv("PGVECTOR_HOST", "localhost")
    monkeypatch.setenv("PGVECTOR_PORT", "5432")
    monkeypatch.setenv("PGVECTOR_USER", "postgres")
    monkeypatch.setenv("PGVECTOR_PASSWORD", "postgres")
    monkeypatch.setenv("PGVECTOR_DATABASE", "vectordb")
    monkeypatch.setenv("PGVECTOR_SSLMODE", "disable")
    # Disable other services
    monkeypatch.setenv("MYSCALE_ENABLED", "false")
    monkeypatch.setenv("CHDB_ENABLED", "false")


@pytest.fixture
def mock_psycopg2():
    """Mock psycopg2 for testing."""
    # Mock both psycopg2 and psycopg2.extras at the import level
    with patch.dict('sys.modules', {
        'psycopg2': MagicMock(),
        'psycopg2.extras': MagicMock()
    }):
        # Import the module to get the mocked psycopg2
        import psycopg2
        from psycopg2.extras import RealDictCursor
        
        # Create mock connection and cursor
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        
        # Set up cursor behavior
        mock_cursor.description = [("id",), ("name",), ("embedding",)]
        mock_cursor.fetchall.return_value = [
            {"id": 1, "name": "item1", "embedding": "[1,2,3]"},
            {"id": 2, "name": "item2", "embedding": "[4,5,6]"},
        ]
        
        mock_conn.cursor.return_value = mock_cursor
        psycopg2.connect = MagicMock(return_value=mock_conn)
        
        yield psycopg2, mock_conn, mock_cursor


def test_pgvector_config(mock_env_vars):
    """Test PGVectorConfig initialization."""
    from mcp_server.config import get_pgvector_config
    
    config = get_pgvector_config()
    
    assert config.enabled is True
    assert config.host == "localhost"
    assert config.port == 5432
    assert config.username == "postgres"
    assert config.password == "postgres"
    assert config.database == "vectordb"
    assert config.sslmode == "disable"
    assert config.connect_timeout == 30


def test_pgvector_config_client_config(mock_env_vars):
    """Test PGVectorConfig get_client_config method."""
    from mcp_server.config import get_pgvector_config
    
    config = get_pgvector_config()
    client_config = config.get_client_config()
    
    assert client_config["host"] == "localhost"
    assert client_config["port"] == 5432
    assert client_config["user"] == "postgres"
    assert client_config["password"] == "postgres"
    assert client_config["database"] == "vectordb"
    assert client_config["sslmode"] == "disable"
    assert client_config["connect_timeout"] == 30


def test_pgvector_config_missing_vars(monkeypatch):
    """Test PGVectorConfig with missing required variables."""
    from mcp_server.config import PGVectorConfig
    
    # Clear all pgvector environment variables first
    monkeypatch.delenv("PGVECTOR_HOST", raising=False)
    monkeypatch.delenv("PGVECTOR_USER", raising=False)
    monkeypatch.delenv("PGVECTOR_PASSWORD", raising=False)
    monkeypatch.delenv("PGVECTOR_DATABASE", raising=False)
    monkeypatch.delenv("PGVECTOR_PORT", raising=False)
    
    monkeypatch.setenv("PGVECTOR_ENABLED", "true")
    # Don't set required variables
    
    with pytest.raises(ValueError, match="Missing required environment variables"):
        PGVectorConfig()


def test_list_pgvector_tables(mock_env_vars, mock_psycopg2):
    """Test list_pgvector_tables function."""
    mock_pg, mock_conn, mock_cursor = mock_psycopg2
    
    # Set up cursor for table listing
    mock_cursor.description = [("table_schema",), ("table_name",), ("table_type",)]
    mock_cursor.fetchall.return_value = [
        {"table_schema": "public", "table_name": "items", "table_type": "BASE TABLE"},
        {"table_schema": "public", "table_name": "documents", "table_type": "BASE TABLE"},
    ]
    
    from mcp_server.pgvector import list_pgvector_tables
    
    result = list_pgvector_tables()
    
    assert "columns" in result
    assert "rows" in result
    assert len(result["rows"]) == 2


def test_list_pgvector_vectors(mock_env_vars, mock_psycopg2):
    """Test list_pgvector_vectors function."""
    mock_pg, mock_conn, mock_cursor = mock_psycopg2
    
    # Set up cursor for vector column listing
    mock_cursor.description = [
        ("table_schema",),
        ("table_name",),
        ("column_name",),
        ("data_type",),
        ("udt_name",),
        ("vector_dimensions",),
    ]
    mock_cursor.fetchall.return_value = [
        {
            "table_schema": "public",
            "table_name": "items",
            "column_name": "embedding",
            "data_type": "vector(3)",
            "udt_name": "vector",
            "vector_dimensions": "3",
        }
    ]
    
    from mcp_server.pgvector import list_pgvector_vectors
    
    result = list_pgvector_vectors()
    
    assert "columns" in result
    assert "rows" in result
    assert len(result["rows"]) == 1


def test_search_similar_vectors(mock_env_vars, mock_psycopg2):
    """Test search_similar_vectors function."""
    mock_pg, mock_conn, mock_cursor = mock_psycopg2
    
    # Set up cursor for similarity search
    mock_cursor.description = [("id",), ("name",), ("distance",)]
    mock_cursor.fetchall.return_value = [
        {"id": 1, "name": "item1", "distance": 0.1},
        {"id": 2, "name": "item2", "distance": 0.5},
    ]
    
    from mcp_server.pgvector import search_similar_vectors
    
    result = search_similar_vectors(
        table_name="items",
        vector_column="embedding",
        query_vector="[1,2,3]",
        limit=5,
        distance_function="l2",
    )
    
    assert "columns" in result
    assert "rows" in result
    assert len(result["rows"]) == 2


def test_search_similar_vectors_invalid_distance(mock_env_vars, mock_psycopg2):
    """Test search_similar_vectors with invalid distance function."""
    from mcp_server.pgvector import search_similar_vectors
    from fastmcp.exceptions import ToolError
    
    with pytest.raises(ToolError, match="Invalid distance function"):
        search_similar_vectors(
            table_name="items",
            vector_column="embedding",
            query_vector="[1,2,3]",
            distance_function="invalid",
        )


def test_run_pgvector_select_query(mock_env_vars, mock_psycopg2):
    """Test run_pgvector_select_query function."""
    mock_pg, mock_conn, mock_cursor = mock_psycopg2
    
    from mcp_server.pgvector import run_pgvector_select_query
    
    query = "SELECT * FROM items LIMIT 5"
    result = run_pgvector_select_query(query)
    
    assert "columns" in result
    assert "rows" in result


def test_pgvector_prompt():
    """Test pgvector_initial_prompt function."""
    from mcp_server.pgvector import pgvector_initial_prompt, PGVECTOR_PROMPT
    
    prompt = pgvector_initial_prompt()
    
    assert prompt == PGVECTOR_PROMPT
    assert "pgvector" in prompt.lower()
    assert "distance" in prompt.lower()

