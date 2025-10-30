"""Environment configuration module for MCP server.

This module handles all environment variable configuration with sensible defaults
and type conversion.
"""

from dataclasses import dataclass
import os
from typing import Optional
from enum import Enum


class TransportType(str, Enum):
    """Supported MCP server transport types."""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"

    @classmethod
    def values(cls) -> list[str]:
        """Get all valid transport values."""
        return [transport.value for transport in cls]


@dataclass
class MyScaleConfig:
    """MyScaleDB connection settings configuration.

    This class handles all environment variable configuration with sensible defaults
    and type conversion. It provides typed methods for accessing each configuration value.

    Required environment variables (only when MYSCALE_ENABLED=true):
        MYSCALE_HOST: The hostname of the MyScaleDB server
        MYSCALE_USER: The username for authentication
        MYSCALE_PASSWORD: The password for authentication

    Optional environment variables (with defaults):
        MYSCALE_PORT: The port number (default: 8443 if secure=True, 8123 if secure=False)
        MYSCALE_SECURE: Enable HTTPS (default: true)
        MYSCALE_VERIFY: Verify SSL certificates (default: true)
        MYSCALE_CONNECT_TIMEOUT: Connection timeout in seconds (default: 30)
        MYSCALE_SEND_RECEIVE_TIMEOUT: Send/receive timeout in seconds (default: 300)
        MYSCALE_DATABASE: Default database to use (default: None)
        MYSCALE_PROXY_PATH: Path to be added to the host URL (default: None)
        MYSCALE_ENABLED: Enable MyScaleDB server (default: true)
    """

    def __init__(self):
        """Initialize the configuration from environment variables."""
        if self.enabled:
            self._validate_required_vars()

    @property
    def enabled(self) -> bool:
        """Get whether MyScaleDB server is enabled.

        Default: True
        """
        return os.getenv("MYSCALE_ENABLED", "true").lower() == "true"

    @property
    def host(self) -> str:
        """Get the MyScaleDB host."""
        return os.environ["MYSCALE_HOST"]

    @property
    def port(self) -> int:
        """Get the MyScaleDB port.

        Defaults to 8443 if secure=True, 8123 if secure=False.
        Can be overridden by MYSCALE_PORT environment variable.
        """
        if "MYSCALE_PORT" in os.environ:
            return int(os.environ["MYSCALE_PORT"])
        return 8443 if self.secure else 8123

    @property
    def username(self) -> str:
        """Get the MyScaleDB username."""
        return os.environ["MYSCALE_USER"]

    @property
    def password(self) -> str:
        """Get the MyScaleDB password."""
        return os.environ["MYSCALE_PASSWORD"]

    @property
    def database(self) -> Optional[str]:
        """Get the default database name if set."""
        return os.getenv("MYSCALE_DATABASE")

    @property
    def secure(self) -> bool:
        """Get whether HTTPS is enabled.

        Default: True (for security)
        """
        return os.getenv("MYSCALE_SECURE", "true").lower() == "true"

    @property
    def verify(self) -> bool:
        """Get whether SSL certificate verification is enabled.

        Default: False
        """
        return os.getenv("MYSCALE_VERIFY", "false").lower() == "true"

    @property
    def connect_timeout(self) -> int:
        """Get the connection timeout in seconds.

        Default: 30
        """
        return int(os.getenv("MYSCALE_CONNECT_TIMEOUT", "30"))

    @property
    def send_receive_timeout(self) -> int:
        """Get the send/receive timeout in seconds.

        Default: 300 (MyScaleDB default)
        """
        return int(os.getenv("MYSCALE_SEND_RECEIVE_TIMEOUT", "300"))

    @property
    def proxy_path(self) -> str:
        return os.getenv("MYSCALE_PROXY_PATH")

    def get_client_config(self) -> dict:
        """Get the configuration dictionary for clickhouse_connect client.

        Returns:
            dict: Configuration ready to be passed to clickhouse_connect.get_client()
        """
        config = {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "password": self.password,
            "interface": "https" if self.secure else "http",
            "secure": self.secure,
            "verify": self.verify,
            "connect_timeout": self.connect_timeout,
            "send_receive_timeout": self.send_receive_timeout,
            "client_name": "mcp_myscaledb",
        }

        # Add optional database if set
        if self.database:
            config["database"] = self.database

        if self.proxy_path:
            config["proxy_path"] = self.proxy_path

        return config

    def _validate_required_vars(self) -> None:
        """Validate that all required environment variables are set.

        Raises:
            ValueError: If any required environment variable is missing.
        """
        missing_vars = []
        for var in ["MYSCALE_HOST", "MYSCALE_USER", "MYSCALE_PASSWORD"]:
            if var not in os.environ:
                missing_vars.append(var)

        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")


@dataclass
class ChDBConfig:
    """chDB connection settings configuration.

    This class handles all environment variable configuration with sensible defaults
    and type conversion.

    Required environment variables:
        CHDB_DATA_PATH: Path to chDB data directory (only required when CHDB_ENABLED=true)
    """

    def __init__(self):
        """Initialize configuration from environment variables."""
        if self.enabled:
            self._validate_required_vars()

    @property
    def enabled(self) -> bool:
        """Get whether chDB is enabled.

        Default: False
        """
        return os.getenv("CHDB_ENABLED", "false").lower() == "true"

    @property
    def data_path(self) -> str:
        """Get chDB data path."""
        return os.getenv("CHDB_DATA_PATH", ":memory:")

    def get_client_config(self) -> dict:
        """Get configuration dictionary for chDB client.

        Returns:
            dict: Configuration ready to be passed to chDB client
        """
        return {
            "data_path": self.data_path,
        }

    def _validate_required_vars(self) -> None:
        """Validate that all required environment variables are set.

        Raises:
            ValueError: If any required environment variable is missing.
        """
        pass


@dataclass
class PGVectorConfig:
    """PostgreSQL with pgvector extension configuration.

    This class handles all environment variable configuration with sensible defaults
    and type conversion.

    Required environment variables (only when PGVECTOR_ENABLED=true):
        PGVECTOR_HOST: PostgreSQL server hostname
        PGVECTOR_PORT: Port number (default: 5432)
        PGVECTOR_USER: Username for authentication
        PGVECTOR_PASSWORD: Password for authentication
        PGVECTOR_DATABASE: Database name

    Optional environment variables (with defaults):
        PGVECTOR_ENABLED: Enable pgvector functionality (default: false)
        PGVECTOR_CONNECT_TIMEOUT: Connection timeout in seconds (default: 30)
        PGVECTOR_SSLMODE: SSL mode for connection (default: prefer)
    """

    def __init__(self):
        """Initialize configuration from environment variables."""
        if self.enabled:
            self._validate_required_vars()

    @property
    def enabled(self) -> bool:
        """Get whether pgvector is enabled.

        Default: False
        """
        return os.getenv("PGVECTOR_ENABLED", "false").lower() == "true"

    @property
    def host(self) -> str:
        """Get PostgreSQL host."""
        return os.environ["PGVECTOR_HOST"]

    @property
    def port(self) -> int:
        """Get PostgreSQL port.

        Default: 5432
        """
        return int(os.getenv("PGVECTOR_PORT", "5432"))

    @property
    def username(self) -> str:
        """Get PostgreSQL username."""
        return os.environ["PGVECTOR_USER"]

    @property
    def password(self) -> str:
        """Get PostgreSQL password."""
        return os.environ["PGVECTOR_PASSWORD"]

    @property
    def database(self) -> str:
        """Get database name."""
        return os.environ["PGVECTOR_DATABASE"]

    @property
    def connect_timeout(self) -> int:
        """Get connection timeout in seconds.

        Default: 30
        """
        return int(os.getenv("PGVECTOR_CONNECT_TIMEOUT", "30"))

    @property
    def sslmode(self) -> str:
        """Get SSL mode for connection.

        Default: prefer
        Valid options: disable, allow, prefer, require, verify-ca, verify-full
        """
        return os.getenv("PGVECTOR_SSLMODE", "prefer")

    def get_client_config(self) -> dict:
        """Get configuration dictionary for psycopg2/asyncpg client.

        Returns:
            dict: Configuration ready to be passed to PostgreSQL client
        """
        return {
            "host": self.host,
            "port": self.port,
            "user": self.username,
            "password": self.password,
            "database": self.database,
            "connect_timeout": self.connect_timeout,
            "sslmode": self.sslmode,
        }

    def _validate_required_vars(self) -> None:
        """Validate that all required environment variables are set.

        Raises:
            ValueError: If any required environment variable is missing.
        """
        missing_vars = []
        for var in [
            "PGVECTOR_HOST",
            "PGVECTOR_USER",
            "PGVECTOR_PASSWORD",
            "PGVECTOR_DATABASE",
        ]:
            if var not in os.environ:
                missing_vars.append(var)

        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")


@dataclass
class MCPServerConfig:
    """MCP server-level settings configuration.

    These settings control server transport and tool behavior,
    intentionally separated from ClickHouse connection validation.

    Optional environment variables (with defaults):
        MCP_SERVER_TRANSPORT: "stdio", "http", or "sse" (default: stdio)
        MCP_BIND_HOST: Bind host for HTTP/SSE (default: 127.0.0.1)
        MCP_BIND_PORT: Bind port for HTTP/SSE (default: 8000)
        MCP_QUERY_TIMEOUT: Timeout for SELECT tools in seconds (default: 30)
    """

    @property
    def server_transport(self) -> str:
        transport = os.getenv("MCP_SERVER_TRANSPORT", TransportType.STDIO.value).lower()
        if transport not in TransportType.values():
            valid_options = ", ".join(f'"{t}"' for t in TransportType.values())
            raise ValueError(f"Invalid transport type '{transport}'. Valid options: {valid_options}")
        return transport

    @property
    def bind_host(self) -> str:
        return os.getenv("MCP_BIND_HOST", "127.0.0.1")

    @property
    def bind_port(self) -> int:
        return int(os.getenv("MCP_BIND_PORT", "8000"))

    @property
    def query_timeout(self) -> int:
        return int(os.getenv("MCP_QUERY_TIMEOUT", "30"))


# Global instance placeholders for the singleton pattern
_MYSCALE_CONFIG_INSTANCE = None
_CHDB_CONFIG_INSTANCE = None
_PGVECTOR_CONFIG_INSTANCE = None
_MCP_CONFIG_INSTANCE = None


def get_myscale_config() -> MyScaleConfig:
    """
    Gets the singleton instance of MyScaleConfig.
    Instantiates it on the first call.
    
    Returns:
        MyScaleConfig: The MyScaleDB configuration instance
    """
    global _MYSCALE_CONFIG_INSTANCE
    if _MYSCALE_CONFIG_INSTANCE is None:
        _MYSCALE_CONFIG_INSTANCE = MyScaleConfig()
    return _MYSCALE_CONFIG_INSTANCE


def get_chdb_config() -> ChDBConfig:
    """
    Gets the singleton instance of ChDBConfig.
    Instantiates it on the first call.

    Returns:
        ChDBConfig: The chDB configuration instance
    """
    global _CHDB_CONFIG_INSTANCE
    if _CHDB_CONFIG_INSTANCE is None:
        _CHDB_CONFIG_INSTANCE = ChDBConfig()
    return _CHDB_CONFIG_INSTANCE


def get_pgvector_config() -> PGVectorConfig:
    """Gets the singleton instance of PGVectorConfig.

    Returns:
        PGVectorConfig: The pgvector configuration instance
    """
    global _PGVECTOR_CONFIG_INSTANCE
    if _PGVECTOR_CONFIG_INSTANCE is None:
        _PGVECTOR_CONFIG_INSTANCE = PGVectorConfig()
    return _PGVECTOR_CONFIG_INSTANCE


def get_mcp_config() -> MCPServerConfig:
    """Gets the singleton instance of MCPServerConfig."""
    global _MCP_CONFIG_INSTANCE
    if _MCP_CONFIG_INSTANCE is None:
        _MCP_CONFIG_INSTANCE = MCPServerConfig()
    return _MCP_CONFIG_INSTANCE

