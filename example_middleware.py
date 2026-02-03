"""
Example middleware module for mcp-clickhouse.

This module demonstrates how to create custom middleware that can be loaded
into the MCP server without modifying the source code.

To use this middleware, set the MCP_MIDDLEWARE_MODULE environment variable:
    MCP_MIDDLEWARE_MODULE=example_middleware

Or in your Claude Desktop config:
    "env": {
        "MCP_MIDDLEWARE_MODULE": "example_middleware",
        ...
    }
"""

import logging
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext

logger = logging.getLogger("example-middleware")


class LoggingMiddleware(Middleware):
    """Example middleware that logs all MCP requests."""
    
    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> any:
        """Log all incoming requests."""
        logger.info(f"Incoming MCP request: method={context.method}, type={context.type}")
        result = await call_next(context)
        logger.info(f"Request completed: method={context.method}")
        return result


class ToolCallLoggingMiddleware(Middleware):
    """Example middleware that specifically logs tool calls."""
    
    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> any:
        """Log tool execution details."""
        tool_name = context.message.name if hasattr(context.message, 'name') else 'unknown'
        logger.info(f"Executing tool: {tool_name}")
        
        try:
            result = await call_next(context)
            logger.info(f"Tool {tool_name} completed successfully")
            return result
        except Exception as e:
            logger.error(f"Tool {tool_name} failed with error: {e}")
            raise


class TimingMiddleware(Middleware):
    """Example middleware that measures request processing time."""
    
    async def on_message(self, context: MiddlewareContext, call_next: CallNext) -> any:
        """Measure processing time for all messages."""
        import time
        start_time = time.time()
        
        result = await call_next(context)
        
        elapsed = time.time() - start_time
        logger.info(f"Request {context.method} took {elapsed:.4f} seconds")
        return result


def setup_middleware(mcp):
    """
    Setup function called by the MCP server to register middleware.
    
    Args:
        mcp: The FastMCP instance
    """
    logger.info("Setting up example middleware")
    
    # Add logging middleware
    mcp.add_middleware(LoggingMiddleware())
    logger.info("Added LoggingMiddleware")
    
    # Add tool-specific logging
    mcp.add_middleware(ToolCallLoggingMiddleware())
    logger.info("Added ToolCallLoggingMiddleware")
    
    # Add timing middleware
    mcp.add_middleware(TimingMiddleware())
    logger.info("Added TimingMiddleware")
    
    logger.info("Example middleware setup complete")
