"""Environment configuration for the MCP ClickHouse server.

This module handles all environment variable configuration with sensible defaults
and type conversion.
"""

from dataclasses import dataclass, field
import os
from typing import Optional, Dict, List


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
    """

    name: str = "default"  # Name identifier
    host: str = None
    port: Optional[int] = None
    username: str = None
    password: str = None
    database: Optional[str] = None
    secure: bool = True
    verify: bool = True
    connect_timeout: int = 30
    send_receive_timeout: int = 300

    def __init__(self, name: str = "default", env_prefix: str = "CLICKHOUSE"):
        """Initialize the configuration from environment variables.
        
        Args:
            name: Name identifier for this ClickHouse connection
            env_prefix: Prefix for environment variables
        """
        self.name = name
        prefix = f"{env_prefix}_" if name == "default" else f"{env_prefix}_{name.upper()}_"
        
        # Set required parameters
        self.host = os.environ.get(f"{prefix}HOST")
        self.username = os.environ.get(f"{prefix}USER")
        self.password = os.environ.get(f"{prefix}PASSWORD")
        
        # Set optional parameters
        port_env = os.environ.get(f"{prefix}PORT")
        if port_env:
            self.port = int(port_env)
            
        self.database = os.environ.get(f"{prefix}DATABASE")
        self.secure = os.environ.get(f"{prefix}SECURE", "true").lower() == "true"
        self.verify = os.environ.get(f"{prefix}VERIFY", "true").lower() == "true"
        self.connect_timeout = int(os.environ.get(f"{prefix}CONNECT_TIMEOUT", "30"))
        self.send_receive_timeout = int(os.environ.get(f"{prefix}SEND_RECEIVE_TIMEOUT", "300"))
        
        if not self.port:
            self.port = 8443 if self.secure else 8123

    def validate(self) -> bool:
        """Validate if the configuration is valid."""
        return bool(self.host and self.username and self.password)

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
            "client_name": f"mcp_clickhouse_{self.name}",
        }

        # Add optional database if set
        if self.database:
            config["database"] = self.database

        return config


@dataclass
class MultiClickHouseConfig:
    """Manage multiple ClickHouse connection configurations."""
    
    configs: Dict[str, ClickHouseConfig] = field(default_factory=dict)
    default_config_name: str = "default"
    
    def __init__(self, allow_empty: bool = False):
        """Initialize all ClickHouse connection configurations from environment variables.
        
        Args:
            allow_empty: If True, don't raise an error when no valid configurations are found.
                         This is useful for testing.
        """
        # Initialize configs dictionary
        self.configs = {}
        self.default_config_name = "default"
        
        # Always try to load the default configuration
        default_config = ClickHouseConfig(name="default")
        if default_config.validate():
            self.configs["default"] = default_config
            self.default_config_name = "default"
        
        # Look for additional servers defined by CLICKHOUSE_SERVERS
        servers_str = os.environ.get("CLICKHOUSE_SERVERS", "")
        if servers_str:
            server_names = [name.strip() for name in servers_str.split(",")]
            for name in server_names:
                if name and name != "default":
                    config = ClickHouseConfig(name=name)
                    if config.validate():
                        self.configs[name] = config
                        # If no valid default configuration, use the first valid one
                        if "default" not in self.configs:
                            self.default_config_name = name
        
        if not self.configs and not allow_empty:
            raise ValueError("No valid ClickHouse configuration found. Please configure at least one valid ClickHouse connection.")
            
    def get_config(self, name: Optional[str] = None) -> ClickHouseConfig:
        """Get a configuration by name, or return the default if not specified or not found.
        
        Args:
            name: Server configuration name
            
        Returns:
            ClickHouse configuration object
        """
        if name and name in self.configs:
            return self.configs[name]
            
        if self.default_config_name in self.configs:
            return self.configs[self.default_config_name]
            
        raise ValueError("No valid ClickHouse configuration found.")
    
    def get_available_servers(self) -> List[str]:
        """Get a list of all available ClickHouse server names.
        
        Returns:
            List of server names
        """
        return list(self.configs.keys())


@dataclass
class MCPServerConfig:
    """MCP server configuration."""
    
    port: int = 8080
    host: str = "0.0.0.0"
    
    def __init__(self):
        """Initialize MCP server configuration from environment variables."""
        if "MCP_SERVER_PORT" in os.environ:
            self.port = int(os.environ["MCP_SERVER_PORT"])
        if "MCP_SERVER_HOST" in os.environ:
            self.host = os.environ["MCP_SERVER_HOST"]


# Global singletons
_MULTI_CONFIG_INSTANCE = None
_MCP_SERVER_CONFIG = None


def get_config(name: Optional[str] = None, allow_empty: bool = False) -> ClickHouseConfig:
    """
    Get a ClickHouse configuration instance.
    
    Args:
        name: Optional configuration name, uses default if not specified
        allow_empty: If True, don't raise an error when no valid configurations are found
        
    Returns:
        ClickHouse configuration instance with the specified name
    """
    global _MULTI_CONFIG_INSTANCE
    if _MULTI_CONFIG_INSTANCE is None:
        _MULTI_CONFIG_INSTANCE = MultiClickHouseConfig(allow_empty=allow_empty)
    return _MULTI_CONFIG_INSTANCE.get_config(name)


def get_all_configs(allow_empty: bool = False) -> MultiClickHouseConfig:
    """Get the multi-ClickHouse configuration management instance.
    
    Args:
        allow_empty: If True, don't raise an error when no valid configurations are found
    
    Returns:
        MultiClickHouseConfig instance
    """
    global _MULTI_CONFIG_INSTANCE
    if _MULTI_CONFIG_INSTANCE is None:
        _MULTI_CONFIG_INSTANCE = MultiClickHouseConfig(allow_empty=allow_empty)
    return _MULTI_CONFIG_INSTANCE


def get_mcp_server_config() -> MCPServerConfig:
    """Get the MCP server configuration instance.
    
    Returns:
        MCPServerConfig instance
    """
    global _MCP_SERVER_CONFIG
    if _MCP_SERVER_CONFIG is None:
        _MCP_SERVER_CONFIG = MCPServerConfig()
    return _MCP_SERVER_CONFIG
