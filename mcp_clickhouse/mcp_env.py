"""Environment configuration for the MCP ClickHouse server.

This module handles all environment variable configuration with sensible defaults
and type conversion.
"""

from dataclasses import dataclass, field
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

    Required environment variables:
        CLICKHOUSE_HOST: The hostname of the ClickHouse server
        CLICKHOUSE_USER: The username for authentication
        CLICKHOUSE_PASSWORD: The password for authentication

    Optional environment variables (with defaults):
        CLICKHOUSE_PORT: The port number (default: 8443 if secure=True, 8123 if secure=False)
        CLICKHOUSE_SECURE: Enable HTTPS (default: true)
        CLICKHOUSE_VERIFY: Verify SSL certificates (default: true)
        CLICKHOUSE_CONNECT_TIMEOUT: Connection timeout in seconds (default: 30)
        CLICKHOUSE_SEND_RECEIVE_TIMEOUT: Send/receive timeout in seconds (default: 300)
        CLICKHOUSE_DATABASE: Default database to use (default: None)
        CLICKHOUSE_PROXY_PATH: Path to be added to the host URL. For instance, for servers behind an HTTP proxy (default: None)
        CLICKHOUSE_MCP_SERVER_TRANSPORT: MCP server transport method - "stdio", "http", or "sse" (default: stdio)
        CLICKHOUSE_MCP_BIND_HOST: Host to bind the MCP server to when using HTTP or SSE transport (default: 127.0.0.1)
        CLICKHOUSE_MCP_BIND_PORT: Port to bind the MCP server to when using HTTP or SSE transport (default: 8000)
        CLICKHOUSE_ENABLED: Enable ClickHouse server (default: true)
    """

    def __init__(self, name: str = "default", env_prefix: str = "CLICKHOUSE"):
        """Initialize the configuration from environment variables.

        Args:
            name: Name identifier for this ClickHouse connection
            env_prefix: Prefix for environment variables
        """
        self.name = name
        self.env_prefix = env_prefix
        if self.enabled:
            self._validate_required_vars()

    def _env_key(self, key: str) -> str:
        """Generate environment variable key based on server name."""
        if self.name == "default":
            return f"{self.env_prefix}_{key}"
        else:
            return f"{self.env_prefix}_{self.name.upper()}_{key}"

    def _get_env(self, key: str, default=None) -> Optional[str]:
        """Get environment variable value."""
        return os.getenv(self._env_key(key), default)

    def _get_bool(self, key: str, default: bool = True) -> bool:
        """Get boolean environment variable value."""
        return self._get_env(key, str(default)).lower() == "true"

    def _get_int(self, key: str, default: int) -> int:
        """Get integer environment variable value."""
        return int(self._get_env(key, str(default)))

    def _validate_required_vars(self):
        """Validate that all required environment variables are set."""
        required = ["HOST", "USER", "PASSWORD"]
        missing = [k for k in required if not self._get_env(k)]
        if missing:
            raise ValueError(f"Missing required ClickHouse env vars for '{self.name}': {missing}")

    @property
    def enabled(self) -> bool:
        """Get whether ClickHouse server is enabled."""
        return self._get_bool("ENABLED", True)

    @property
    def host(self) -> str:
        """Get the ClickHouse hostname."""
        return self._get_env("HOST")

    @property
    def port(self) -> int:
        """Get the ClickHouse port number."""
        if self._get_env("PORT") is not None:
            return self._get_int("PORT", 8123)
        return 8443 if self.secure else 8123

    @property
    def username(self) -> str:
        """Get the ClickHouse username."""
        return self._get_env("USER")

    @property
    def password(self) -> str:
        """Get the ClickHouse password."""
        return self._get_env("PASSWORD")

    @property
    def database(self) -> Optional[str]:
        """Get the default database name."""
        return self._get_env("DATABASE")

    @property
    def secure(self) -> bool:
        """Get whether to use HTTPS connection."""
        return self._get_bool("SECURE", True)

    @property
    def verify(self) -> bool:
        """Get whether to verify SSL certificates."""
        return self._get_bool("VERIFY", True)

    @property
    def connect_timeout(self) -> int:
        """Get the connection timeout in seconds."""
        return self._get_int("CONNECT_TIMEOUT", 30)

    @property
    def send_receive_timeout(self) -> int:
        """Get the send/receive timeout in seconds."""
        return self._get_int("SEND_RECEIVE_TIMEOUT", 300)

    @property
    def proxy_path(self) -> Optional[str]:
        """Get the proxy path."""
        return self._get_env("PROXY_PATH")

    @property
    def mcp_server_transport(self) -> str:
        """Get the MCP server transport method."""
        transport = os.getenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", TransportType.STDIO.value).lower()
        if transport not in TransportType.values():
            valid = ", ".join(f'"{t}"' for t in TransportType.values())
            raise ValueError(f"Invalid transport '{transport}'. Valid: {valid}")
        return transport

    @property
    def mcp_bind_host(self) -> str:
        """Get the host to bind the MCP server to."""
        return os.getenv("CLICKHOUSE_MCP_BIND_HOST", "127.0.0.1")

    @property
    def mcp_bind_port(self) -> int:
        """Get the port to bind the MCP server to."""
        return int(os.getenv("CLICKHOUSE_MCP_BIND_PORT", "8000"))

    def get_client_config(self) -> dict:
        """Get the configuration dictionary for clickhouse_connect client."""
        cfg = {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "password": self.password,
            "secure": self.secure,
            "verify": self.verify,
            "connect_timeout": self.connect_timeout,
            "send_receive_timeout": self.send_receive_timeout,
            "client_name": f"mcp_clickhouse_{self.name}",
        }
        if self.database:
            cfg["database"] = self.database
        if self.proxy_path:
            cfg["proxy_path"] = self.proxy_path
        return cfg

    def validate(self) -> bool:
        """Validate if configuration is complete."""
        return bool(self.host and self.username and self.password)


@dataclass
class MultiClickHouseConfig:
    """Manager for multiple ClickHouse connection configurations."""
    configs: Dict[str, ClickHouseConfig] = field(default_factory=dict)
    default_config_name: str = "default"

    def __init__(self, allow_empty: bool = False):
        """Initialize all ClickHouse connection configurations from environment variables."""
        self.configs = {}
        self.default_config_name = None

        # Load server names from CLICKHOUSE_SERVERS environment variable
        servers_str = os.getenv("CLICKHOUSE_SERVERS", "")
        names = [n.strip() for n in servers_str.split(",")] if servers_str else []

        # Process named servers first
        for name in names:
            if name and name != "default":
                cfg = ClickHouseConfig(name=name)
                if cfg.validate():
                    self.configs[name] = cfg
                    if self.default_config_name is None:
                        self.default_config_name = name

        # Try to load default configuration
        if self.default_config_name is None:
            default_cfg = ClickHouseConfig(name="default")
            if default_cfg.validate():
                self.configs["default"] = default_cfg
                self.default_config_name = "default"

        if not self.configs and not allow_empty:
            raise ValueError("No valid ClickHouse configuration found.")

    def get_config(self, name: Optional[str] = None) -> ClickHouseConfig:
        """Get configuration by name, or default if name not specified or not found."""
        if name and name in self.configs:
            return self.configs[name]
        if self.default_config_name and self.default_config_name in self.configs:
            return self.configs[self.default_config_name]
        raise ValueError("No valid ClickHouse configuration found.")

    def get_available_servers(self) -> List[str]:
        """Get list of all available ClickHouse server names."""
        return list(self.configs.keys())


@dataclass
class ChDBConfig:
    """Configuration for chDB connection settings."""

    def __init__(self):
        """Initialize the configuration from environment variables."""
        if self.enabled:
            self._validate_required_vars()

    @property
    def enabled(self) -> bool:
        """Get whether chDB is enabled."""
        return os.getenv("CHDB_ENABLED", "false").lower() == "true"

    @property
    def data_path(self) -> str:
        """Get the chDB data path."""
        return os.getenv("CHDB_DATA_PATH", ":memory:")

    def get_client_config(self) -> dict:
        """Get the configuration dictionary for chDB client."""
        return {"data_path": self.data_path}

    def _validate_required_vars(self) -> None:
        """Validate that all required environment variables are set."""
        # Only data_path is optional, no required vars
        pass


@dataclass
class MCPServerConfig:
    """MCP server configuration."""
    port: int = 8080
    host: str = "0.0.0.0"

    def __init__(self):
        """Initialize MCP server configuration from environment variables."""
        if os.getenv("MCP_SERVER_PORT"):
            self.port = int(os.getenv("MCP_SERVER_PORT"))
        if os.getenv("MCP_SERVER_HOST"):
            self.host = os.getenv("MCP_SERVER_HOST")


# Global singletons
_MULTI_CONFIG_INSTANCE = None
_MCP_SERVER_CONFIG = None
_CONFIG_INSTANCE = None
_CHDB_CONFIG_INSTANCE = None


def get_config(name: Optional[str] = None, allow_empty: bool = False) -> ClickHouseConfig:
    """Get ClickHouse configuration instance.

    Args:
        name: Optional configuration name, uses default if not specified
        allow_empty: Allow empty configurations for testing

    Returns:
        ClickHouse configuration instance for the specified name
    """
    global _MULTI_CONFIG_INSTANCE
    if _MULTI_CONFIG_INSTANCE is None:
        _MULTI_CONFIG_INSTANCE = MultiClickHouseConfig(allow_empty=allow_empty)
    return _MULTI_CONFIG_INSTANCE.get_config(name)


def get_all_configs(allow_empty: bool = False) -> MultiClickHouseConfig:
    """Get multi-ClickHouse configuration manager instance."""
    global _MULTI_CONFIG_INSTANCE
    if _MULTI_CONFIG_INSTANCE is None:
        _MULTI_CONFIG_INSTANCE = MultiClickHouseConfig(allow_empty=allow_empty)
    return _MULTI_CONFIG_INSTANCE


def get_mcp_server_config() -> MCPServerConfig:
    """Get MCP server configuration instance."""
    global _MCP_SERVER_CONFIG
    if _MCP_SERVER_CONFIG is None:
        _MCP_SERVER_CONFIG = MCPServerConfig()
    return _MCP_SERVER_CONFIG


def get_chdb_config() -> ChDBConfig:
    """Get chDB configuration instance."""
    global _CHDB_CONFIG_INSTANCE
    if _CHDB_CONFIG_INSTANCE is None:
        _CHDB_CONFIG_INSTANCE = ChDBConfig()
    return _CHDB_CONFIG_INSTANCE
