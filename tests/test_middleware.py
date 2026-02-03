import pytest
import os
from unittest.mock import Mock, patch
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext
from mcp_clickhouse.mcp_middleware_hook import setup_middleware


class TestMiddlewareLoading:
    """Test the custom middleware loading system."""

    def test_middleware_module_not_set(self):
        """Test that the server starts normally when no middleware is configured."""
        # Test the middleware loading logic in isolation
        mock_mcp = Mock()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_MIDDLEWARE_MODULE", None)
            setup_middleware(mock_mcp)

            assert not mock_mcp.add_middleware.called

    def test_middleware_import_logic(self):
        """Test the middleware import and setup logic."""
        with patch.dict(os.environ, {"MCP_MIDDLEWARE_MODULE": "test_middleware"}):
            mock_mcp = Mock()
            mock_module = Mock()
            mock_module.setup_middleware = Mock()

            with patch("importlib.import_module", return_value=mock_module) as mock_import:
                # Simulate the middleware loading logic from main.py
                setup_middleware(mock_mcp)
                # Verify the module was imported
                mock_import.assert_called_once_with("test_middleware")

                # Verify setup_middleware was called
                mock_module.setup_middleware.assert_called_once_with(mock_mcp)

    def test_middleware_without_setup_function(self):
        """Test handling of module without setup_middleware."""
        with patch.dict(os.environ, {"MCP_MIDDLEWARE_MODULE": "incomplete_middleware"}):
            mock_module = Mock(spec=[])  # Empty spec, no setup_middleware

            with patch("importlib.import_module", return_value=mock_module):
                mock_mcp = Mock()
                # this should not raise an error
                setup_middleware(mock_mcp)
                assert not mock_mcp.add_middleware.called

    def test_middleware_import_error(self):
        """Test handling of import errors."""
        with patch.dict(os.environ, {"MCP_MIDDLEWARE_MODULE": "nonexistent_module"}):
            with patch("importlib.import_module", side_effect=ImportError("No module")):
                mock_mcp = Mock()
                with pytest.raises(ImportError):
                    setup_middleware(mock_mcp)
                    assert not mock_mcp.add_middleware.called

    def test_middleware_setup_error(self):
        """Test handling of errors during setup."""
        with patch.dict(os.environ, {"MCP_MIDDLEWARE_MODULE": "broken_middleware"}):
            mock_mcp = Mock()
            mock_module = Mock()
            mock_module.setup_middleware = Mock(side_effect=Exception("Setup failed"))

            with patch("importlib.import_module", return_value=mock_module):
                # Simulate the middleware loading logic
                with pytest.raises(Exception, match="Setup failed"):
                    setup_middleware(mock_mcp)
                    assert not mock_mcp.add_middleware.called


class TestMiddlewareIntegration:
    """Integration tests for the middleware system."""

    def test_middleware_can_be_added_to_mcp(self):
        """Test that middleware can be added to the FastMCP instance."""
        from mcp_clickhouse.mcp_server import mcp

        # Create a simple middleware
        class TestMiddleware(Middleware):
            async def on_message(self, context: MiddlewareContext, call_next: CallNext):
                return await call_next(context)

        # Add middleware to the mcp instance
        initial_count = len(mcp.middleware)
        mcp.add_middleware(TestMiddleware())

        # Verify middleware was added
        assert len(mcp.middleware) == initial_count + 1
        assert isinstance(mcp.middleware[-1], TestMiddleware)

    def test_custom_middleware_example(self):
        """Test a realistic example of custom middleware setup."""
        from mcp_clickhouse.mcp_server import mcp

        # Simulate what a user's middleware module would look like
        class LoggingMiddleware(Middleware):
            """Example middleware that logs tool calls."""

            async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
                # Log the tool call
                result = await call_next(context)
                return result

        # Call the setup function
        initial_count = len(mcp.middleware)

        def setup_user_middleware(mcp_instance):
            mcp_instance.add_middleware(LoggingMiddleware())

        with patch.dict(os.environ, {"MCP_MIDDLEWARE_MODULE": "user_middleware"}):
            mock_module = Mock()
            mock_module.setup_middleware = setup_user_middleware
            with patch("importlib.import_module", return_value=mock_module):
                setup_middleware(mcp)

        # Verify middleware was registered
        assert len(mcp.middleware) == initial_count + 1
        assert isinstance(mcp.middleware[-1], LoggingMiddleware)
