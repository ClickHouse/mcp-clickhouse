import logging
import os
import importlib

logger = logging.getLogger("mcp-clickhouse")


def setup_middleware(mcp):
    # Load custom middleware if specified
    middleware_module = os.getenv("MCP_MIDDLEWARE_MODULE")
    if middleware_module:
        try:
            logger.info(f"Loading middleware module: {middleware_module}")
            mod = importlib.import_module(middleware_module)
            if hasattr(mod, 'setup_middleware'):
                logger.info("Found setup_middleware function, calling it")
                mod.setup_middleware(mcp)
            else:
                logger.warning(f"Middleware module '{middleware_module}' does not have a 'setup_middleware' function")
        except ImportError as e:
            logger.error(f"Failed to import middleware module '{middleware_module}': {e}")
        except Exception as e:
            logger.error(f"Failed to load middleware: {e}")
    return mcp
