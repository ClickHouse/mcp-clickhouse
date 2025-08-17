"""Environment configuration for the MCP ClickHouse server with Multi-Tenancy support.

This module handles all environment variable configuration with sensible defaults
and type conversion.
"""

from dataclasses import dataclass
import os
from typing import Optional, Dict, List
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
class ClickHouseConfig:
    """Configuration for ClickHouse connection settings.

    This class handles all environment variable configuration with sensible defaults
    and type conversion. It provides typed methods for accessing each configuration value.

    Required environment variables (only when CH_<tenant>_CLICKHOUSE_ENABLED=true):
        CH_<tenant>_CLICKHOUSE_HOST: The hostname of the ClickHouse server
        CH_<tenant>_CLICKHOUSE_USER: The username for authentication
        CH_<tenant>_CLICKHOUSE_PASSWORD: The password for authentication

    Optional environment variables (with defaults):
        CH_<tenant>_CLICKHOUSE_PORT: The port number (default: 8443 if secure=True, 8123 if secure=False)
        CH_<tenant>_CLICKHOUSE_SECURE: Enable HTTPS (default: true)
        CH_<tenant>_CLICKHOUSE_VERIFY: Verify SSL certificates (default: true)
        CH_<tenant>_CLICKHOUSE_CONNECT_TIMEOUT: Connection timeout in seconds (default: 30)
        CH_<tenant>_CLICKHOUSE_SEND_RECEIVE_TIMEOUT: Send/receive timeout in seconds (default: 300)
        CH_<tenant>_CLICKHOUSE_DATABASE: Default database to use (default: None)
        CH_<tenant>_CLICKHOUSE_PROXY_PATH: Path to be added to the host URL. For instance, for servers behind an HTTP proxy (default: None)
        CH_<tenant>_CLICKHOUSE_MCP_SERVER_TRANSPORT: MCP server transport method - "stdio", "http", or "sse" (default: stdio)
        CH_<tenant>_CLICKHOUSE_MCP_BIND_HOST: Host to bind the MCP server to when using HTTP or SSE transport (default: 127.0.0.1)
        CH_<tenant>_CLICKHOUSE_MCP_BIND_PORT: Port to bind the MCP server to when using HTTP or SSE transport (default: 8000)
        CH_<tenant>_CLICKHOUSE_ENABLED: Enable ClickHouse server (default: true)
    """
    tenant: str

    def __init__(self):
        """Initialize the configuration from environment variables."""
        if self.enabled:
            self._validate_required_vars()
            
    def _getenv(self, key: str, default=None, cast=str):
        prefixed_key = f"CH_{self.tenant}_{key}"
        val = os.getenv(prefixed_key, os.getenv(key, default))
        if val is not None and cast is not str:
            try:
                return cast(val)
            except Exception:
                raise ValueError(f"Invalid value for {prefixed_key or key}: {val}")
        return val

    @property
    def enabled(self) -> bool:
        """Get whether ClickHouse server is enabled.

        Default: True
        """
        return self._getenv("CLICKHOUSE_ENABLED", "true", cast=lambda v: v.lower() == "true")

    @property
    def host(self) -> str:
        """Get the ClickHouse host."""
        return self._getenv("CLICKHOUSE_HOST")

    @property
    def port(self) -> int:
        """Get the ClickHouse port.

        Defaults to 8443 if secure=True, 8123 if secure=False.
        Can be overridden by CLICKHOUSE_PORT environment variable.
        """
        default = 8443 if self.secure else 8123
        return self._getenv("CLICKHOUSE_PORT", default, cast=int)

    @property
    def username(self) -> str:
        """Get the ClickHouse username."""
        return self._getenv("CLICKHOUSE_USER")

    @property
    def password(self) -> str:
        """Get the ClickHouse password."""
        return self._getenv("CLICKHOUSE_PASSWORD")

    @property
    def database(self) -> Optional[str]:
        """Get the default database name if set."""
        return self._getenv("CLICKHOUSE_DATABASE")

    @property
    def secure(self) -> bool:
        """Get whether HTTPS is enabled.

        Default: True
        """
        return self._getenv("CLICKHOUSE_SECURE", "true", cast=lambda v: v.lower() == "true")

    @property
    def verify(self) -> bool:
        """Get whether SSL certificate verification is enabled.

        Default: True
        """
        return self._getenv("CLICKHOUSE_VERIFY", "true", cast=lambda v: v.lower() == "true")

    @property
    def connect_timeout(self) -> int:
        """Get the connection timeout in seconds.

        Default: 30
        """
        return self._getenv("CLICKHOUSE_CONNECT_TIMEOUT", 30, cast=int)

    @property
    def send_receive_timeout(self) -> int:
        """Get the send/receive timeout in seconds.

        Default: 300 (ClickHouse default)
        """
        return self._getenv("CLICKHOUSE_SEND_RECEIVE_TIMEOUT", 300, cast=int)

    @property
    def proxy_path(self) -> Optional[str]:
        return self._getenv("CLICKHOUSE_PROXY_PATH")

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
            "secure": self.secure,
            "verify": self.verify,
            "connect_timeout": self.connect_timeout,
            "send_receive_timeout": self.send_receive_timeout,
            "client_name": f"{self.tenant}mcp_clickhouse",
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
        for var in ["CLICKHOUSE_HOST", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"]:
            if not self._getenv(var):
                missing_vars.append(var)
        if missing_vars:
            raise ValueError(f"Missing required environment variables for tenant '{self.tenant}': {', '.join(missing_vars)}")


@dataclass
class ChDBConfig:
    """Configuration for chDB connection settings.

    This class handles all environment variable configuration with sensible defaults
    and type conversion. It provides typed methods for accessing each configuration value.

    Required environment variables:
        CHDB_DATA_PATH: The path to the chDB data directory (only required if CHDB_ENABLED=true)
    """
    tenant: str

    def __init__(self):
        """Initialize the configuration from environment variables."""
        if self.enabled:
            self._validate_required_vars()

    def _getenv(self, key: str, default=None, cast=str):
        prefixed_key = f"CH_{self.tenant}_{key}"
        val = os.getenv(prefixed_key, os.getenv(key, default))
        if val is not None and cast is not str:
            try:
                return cast(val)
            except Exception:
                raise ValueError(f"Invalid value for {prefixed_key or key}: {val}")
        return val

    @property
    def enabled(self) -> bool:
        """Get whether chDB is enabled.

        Default: False
        """
        return self._getenv("CHDB_ENABLED", "false", cast=lambda v: v.lower() == "true")

    @property
    def data_path(self) -> str:
        """Get the chDB data path."""
        return self._getenv("CHDB_DATA_PATH", ":memory:")

    def get_client_config(self) -> dict:
        """Get the configuration dictionary for chDB client.

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

def get_mcp_config() -> dict:
    """
    Get the MCP server configuration from environment variables.
    """
    # Global MCP transport config
    MCP_TRANSPORT = os.getenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", TransportType.STDIO.value).lower()
    if MCP_TRANSPORT not in TransportType.values():
        raise ValueError(f"Invalid MCP transport '{MCP_TRANSPORT}'. Valid options: {TransportType.values()}")

    MCP_BIND_HOST = os.getenv("CLICKHOUSE_MCP_BIND_HOST", "127.0.0.1")
    MCP_BIND_PORT = int(os.getenv("CLICKHOUSE_MCP_BIND_PORT", 8000))

    return {
        "mcp_server_transport": MCP_TRANSPORT,
        "mcp_bind_host": MCP_BIND_HOST,
        "mcp_bind_port": MCP_BIND_PORT,
    }

# Global instance placeholders for the singleton pattern
_CLICKHOUSE_TENANTS: Dict[str, ClickHouseConfig] = {}
_CHDB_TENANTS: Dict[str, ChDBConfig] = {}

def load_clickhouse_configs() -> Dict[str, ClickHouseConfig]:
    global _CLICKHOUSE_TENANTS
    for key in os.environ:
        if key.endswith("CLICKHOUSE_HOST") and key.startswith("CH_"):
            # CH_<tenant>_CLICKHOUSE_HOST 
            tenant = key[len("CH_"): -len("_CLICKHOUSE_HOST")]
            _CLICKHOUSE_TENANTS[tenant] = ClickHouseConfig(tenant=tenant)
    if not _CLICKHOUSE_TENANTS and "CLICKHOUSE_HOST" in os.environ:
        _CLICKHOUSE_TENANTS["default"] = ClickHouseConfig(tenant="")
    
    return _CLICKHOUSE_TENANTS

def load_chdb_configs() -> Dict[str, ChDBConfig]:
    global _CHDB_TENANTS
    for key in os.environ:
        if key.endswith("CHDB_DATA_PATH") and key.startswith("CH_"):
            # CH_<tenant>_CLICKHOUSE_HOST 
            tenant = key[len("CH_"): -len("_CHDB_DATA_PATH")]
            _CHDB_TENANTS[tenant] = ChDBConfig(tenant=tenant)
    if not _CHDB_TENANTS and "CHDB_DATA_PATH" in os.environ:
        _CHDB_TENANTS["default"] = ChDBConfig(tenant="")
    return _CHDB_TENANTS

def get_config(tenant: str = "default") -> ClickHouseConfig:
    """Get ClickHouseConfig for a specific tenant."""
    global _CLICKHOUSE_TENANTS
    
    # Check for tenant in the global config map
    if tenant not in _CLICKHOUSE_TENANTS:
        raise ValueError(f"No ClickHouse config found for tenant '{tenant}'")
    
    return _CLICKHOUSE_TENANTS[tenant]

def get_chdb_config(tenant: str = "default") -> ChDBConfig:
    """Get ChDBConfig for a specific tenant."""
    global _CHDB_TENANTS

    # Check for tenant in the global config map
    if tenant not in _CHDB_TENANTS:
        raise ValueError(f"No ChDB config found for tenant '{tenant}'")
    return _CHDB_TENANTS[tenant]

def list_clickhouse_tenants() -> List[str]:
    """Get list of all clickhouse tenant names."""
    global _CLICKHOUSE_TENANTS
    return [tenant for tenant in _CLICKHOUSE_TENANTS.keys()]

def list_chdb_tenants() -> List[str]:
    """Get list of all chdb tenant names."""
    global _CHDB_TENANTS
    return [tenant for tenant in _CHDB_TENANTS.keys()]