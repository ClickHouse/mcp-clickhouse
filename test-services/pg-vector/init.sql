-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Create a sample table with vector column
CREATE TABLE IF NOT EXISTS items (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    embedding vector(3),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Insert sample data
INSERT INTO items (name, description, embedding) VALUES
    ('item1', 'First test item', '[1,2,3]'),
    ('item2', 'Second test item', '[4,5,6]'),
    ('item3', 'Third test item', '[7,8,9]'),
    ('item4', 'Fourth test item', '[1,1,1]'),
    ('item5', 'Fifth test item', '[2,2,2]');

-- Create index for vector similarity search (HNSW)
CREATE INDEX ON items USING hnsw (embedding vector_l2_ops);

-- Create another example table with higher dimensional vectors
CREATE TABLE IF NOT EXISTS documents (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT,
    embedding vector(1536),  -- Common dimension for OpenAI embeddings
    metadata JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create index for documents
CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops);

-- Create a table for demonstrating different distance functions
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT,
    embedding vector(128),
    price DECIMAL(10, 2),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes with different distance functions
CREATE INDEX products_l2_idx ON products USING hnsw (embedding vector_l2_ops);
CREATE INDEX products_cosine_idx ON products USING hnsw (embedding vector_cosine_ops);
CREATE INDEX products_ip_idx ON products USING hnsw (embedding vector_ip_ops);

-- Grant privileges
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO postgres;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO postgres;

-- Display info
SELECT 'pgvector extension installed successfully' as status;
SELECT 'Sample tables created: items, documents, products' as info;

