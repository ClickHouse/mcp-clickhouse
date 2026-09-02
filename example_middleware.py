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
import time
from typing import Any

from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext

from mcp_clickhouse.mcp_server import CLIENT_CONFIG_OVERRIDES_KEY

logger = logging.getLogger("example-middleware")


class LoggingMiddleware(Middleware):
    """Example middleware that logs all MCP requests."""

    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        """Log all incoming requests."""
        logger.info(f"Incoming MCP request: method={context.method}, type={context.type}")
        result = await call_next(context)
        logger.info(f"Request completed: method={context.method}")
        return result


class ToolCallLoggingMiddleware(Middleware):
    """Example middleware that specifically logs tool calls."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        """Log tool execution details."""
        tool_name = context.message.name if hasattr(context.message, "name") else "unknown"
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

    async def on_message(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        """Measure processing time for all messages."""
        start_time = time.time()

        result = await call_next(context)

        elapsed = time.time() - start_time
        logger.info(f"Request {context.method} took {elapsed:.4f} seconds")
        return result


class ClientConfigOverrideMiddleware(Middleware):
    """Example middleware that overrides ClickHouse client configuration per request.

    This uses FastMCP's asynchronous context state to pass a request-scoped
    override to the server. `serializable=False` is mandatory: FastMCP 4's
    default `set_state` writes to a session-scoped store keyed by
    `mcp-session-id` with a 24 hour TTL, so without it this override would
    apply to every later tool call in the same streamable HTTP session, not
    just this one. `serializable=False` stores the value in
    FastMCP's request-scoped state instead, which lives only for the current
    tool call.

    This class is a template and is deliberately not registered by
    `setup_middleware` below: registering it would replace the configured
    CLICKHOUSE_CONNECT_TIMEOUT and CLICKHOUSE_SEND_RECEIVE_TIMEOUT on every tool
    call for anyone who enables the example module. Copy it into your own
    middleware module and call `mcp.add_middleware(ClientConfigOverrideMiddleware())`
    from that module's `setup_middleware`.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        """Set a fixed timeout override for the duration of this tool call."""
        ctx = get_context()
        await ctx.set_state(
            CLIENT_CONFIG_OVERRIDES_KEY,
            {"connect_timeout": 60, "send_receive_timeout": 120},
            serializable=False,
        )
        return await call_next(context)


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

    # ClientConfigOverrideMiddleware is intentionally not registered here; see
    # its docstring.

    logger.info("Example middleware setup complete")
