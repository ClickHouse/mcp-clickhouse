import uvicorn
from mcp_clickhouse.mcp_server import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=18213)