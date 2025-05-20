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

    name: str = "default"  # 添加名称标识符
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
        
        # 设置必选参数
        self.host = os.environ.get(f"{prefix}HOST")
        self.username = os.environ.get(f"{prefix}USER")
        self.password = os.environ.get(f"{prefix}PASSWORD")
        
        # 设置可选参数
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
        """验证配置是否有效"""
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
    """管理多个ClickHouse连接配置"""
    
    configs: Dict[str, ClickHouseConfig] = field(default_factory=dict)
    default_config_name: str = "default"
    
    def __init__(self):
        """从环境变量初始化所有ClickHouse连接配置"""
        # 始终尝试加载默认配置
        default_config = ClickHouseConfig(name="default")
        if default_config.validate():
            self.configs["default"] = default_config
            self.default_config_name = "default"
        
        # 查找以CLICKHOUSE_SERVERS定义的其他服务器
        servers_str = os.environ.get("CLICKHOUSE_SERVERS", "")
        if servers_str:
            server_names = [name.strip() for name in servers_str.split(",")]
            for name in server_names:
                if name and name != "default":
                    config = ClickHouseConfig(name=name)
                    if config.validate():
                        self.configs[name] = config
                        # 如果没有有效的默认配置，则使用第一个有效配置作为默认值
                        if "default" not in self.configs:
                            self.default_config_name = name
        
        if not self.configs:
            raise ValueError("未找到有效的ClickHouse配置。请至少设置一个有效的ClickHouse连接。")
            
    def get_config(self, name: Optional[str] = None) -> ClickHouseConfig:
        """获取指定名称的配置，如果未指定或不存在则返回默认配置"""
        if name and name in self.configs:
            return self.configs[name]
        return self.configs[self.default_config_name]
    
    def get_available_servers(self) -> List[str]:
        """获取所有可用的ClickHouse服务器名称列表"""
        return list(self.configs.keys())


@dataclass
class MCPServerConfig:
    """MCP服务器配置"""
    
    port: int = 8080
    host: str = "0.0.0.0"
    
    def __init__(self):
        """从环境变量初始化MCP服务器配置"""
        if "MCP_SERVER_PORT" in os.environ:
            self.port = int(os.environ["MCP_SERVER_PORT"])
        if "MCP_SERVER_HOST" in os.environ:
            self.host = os.environ["MCP_SERVER_HOST"]


# 全局单例
_MULTI_CONFIG_INSTANCE = None
_MCP_SERVER_CONFIG = None


def get_config(name: Optional[str] = None) -> ClickHouseConfig:
    """
    获取ClickHouse配置实例
    
    Args:
        name: 可选的配置名称，如果未指定则使用默认配置
        
    Returns:
        指定名称的ClickHouse配置实例
    """
    global _MULTI_CONFIG_INSTANCE
    if _MULTI_CONFIG_INSTANCE is None:
        _MULTI_CONFIG_INSTANCE = MultiClickHouseConfig()
    return _MULTI_CONFIG_INSTANCE.get_config(name)


def get_all_configs() -> MultiClickHouseConfig:
    """获取多ClickHouse配置管理实例"""
    global _MULTI_CONFIG_INSTANCE
    if _MULTI_CONFIG_INSTANCE is None:
        _MULTI_CONFIG_INSTANCE = MultiClickHouseConfig()
    return _MULTI_CONFIG_INSTANCE


def get_mcp_server_config() -> MCPServerConfig:
    """获取MCP服务器配置实例"""
    global _MCP_SERVER_CONFIG
    if _MCP_SERVER_CONFIG is None:
        _MCP_SERVER_CONFIG = MCPServerConfig()
    return _MCP_SERVER_CONFIG
