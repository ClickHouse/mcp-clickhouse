#!/bin/bash

# Single command to build and run the MCP ClickHouse server in Docker
# Usage: ./docker-run.sh [mode] [port]
# mode: http (default) or stream
# port: server port (default: 8213)

set -e

# Parse arguments
MODE=${1:-http}
PORT=${2:-8213}

# Validate mode
if [[ "$MODE" != "http" && "$MODE" != "stream" && "$MODE" != "streaming" ]]; then
    echo "Error: Invalid mode '$MODE'. Use 'http' or 'stream'."
    exit 1
fi

echo "Building MCP ClickHouse Docker image..."
docker build -t mcp-clickhouse .

echo "Starting MCP ClickHouse server in $MODE mode on port $PORT..."

# Set environment variables for ClickHouse connection
# These should be customized for your environment
CLICKHOUSE_ENV_VARS=""
if [[ -f ".env" ]]; then
    echo "Loading environment variables from .env file..."
    CLICKHOUSE_ENV_VARS="--env-file .env"
else
    echo "No .env file found. Using default ClickHouse playground settings..."
    CLICKHOUSE_ENV_VARS="
        -e CLICKHOUSE_HOST=sql-clickhouse.clickhouse.com
        -e CLICKHOUSE_PORT=8443
        -e CLICKHOUSE_USER=demo
        -e CLICKHOUSE_PASSWORD=
        -e CLICKHOUSE_SECURE=true
        -e CLICKHOUSE_VERIFY=true
        -e CLICKHOUSE_CONNECT_TIMEOUT=30
        -e CLICKHOUSE_SEND_RECEIVE_TIMEOUT=30
    "
fi

# Run the container
docker run -it --rm \
    -p "$PORT:8213" \
    -e MCP_SERVER_MODE="$MODE" \
    -e MCP_SERVER_HOST=0.0.0.0 \
    -e MCP_SERVER_PORT=8213 \
    $CLICKHOUSE_ENV_VARS \
    mcp-clickhouse

echo "Container stopped."