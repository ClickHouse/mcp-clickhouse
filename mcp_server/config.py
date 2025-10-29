"""环境配置模块，用于 MCP 服务器。

该模块处理所有环境变量配置，提供合理的默认值和类型转换。
"""

from dataclasses import dataclass
import os
from typing import Optional
from enum import Enum


class TransportType(str, Enum):
    """支持的 MCP 服务器传输类型。"""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"

    @classmethod
    def values(cls) -> list[str]:
        """获取所有有效的传输值。"""
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
    """chDB 连接设置配置。

    该类处理所有环境变量配置，提供合理的默认值和类型转换。

    必需的环境变量：
        CHDB_DATA_PATH: chDB 数据目录的路径（仅在 CHDB_ENABLED=true 时需要）
    """

    def __init__(self):
        """从环境变量初始化配置。"""
        if self.enabled:
            self._validate_required_vars()

    @property
    def enabled(self) -> bool:
        """获取 chDB 是否启用。

        默认值：False
        """
        return os.getenv("CHDB_ENABLED", "false").lower() == "true"

    @property
    def data_path(self) -> str:
        """获取 chDB 数据路径。"""
        return os.getenv("CHDB_DATA_PATH", ":memory:")

    def get_client_config(self) -> dict:
        """获取 chDB 客户端的配置字典。

        返回：
            dict: 准备传递给 chDB 客户端的配置
        """
        return {
            "data_path": self.data_path,
        }

    def _validate_required_vars(self) -> None:
        """验证所有必需的环境变量是否已设置。

        抛出：
            ValueError: 如果缺少任何必需的环境变量。
        """
        pass


@dataclass
class PGVectorConfig:
    """PostgreSQL with pgvector 扩展配置。

    该类处理所有环境变量配置，提供合理的默认值和类型转换。

    必需的环境变量（仅当 PGVECTOR_ENABLED=true 时）：
        PGVECTOR_HOST: PostgreSQL 服务器的主机名
        PGVECTOR_PORT: 端口号（默认：5432）
        PGVECTOR_USER: 认证用户名
        PGVECTOR_PASSWORD: 认证密码
        PGVECTOR_DATABASE: 数据库名称

    可选的环境变量（带默认值）：
        PGVECTOR_ENABLED: 启用 pgvector 功能（默认：false）
        PGVECTOR_CONNECT_TIMEOUT: 连接超时时间（秒）（默认：30）
        PGVECTOR_SSLMODE: 连接的 SSL 模式（默认：prefer）
    """

    def __init__(self):
        """从环境变量初始化配置。"""
        if self.enabled:
            self._validate_required_vars()

    @property
    def enabled(self) -> bool:
        """获取 pgvector 是否启用。

        默认值：False
        """
        return os.getenv("PGVECTOR_ENABLED", "false").lower() == "true"

    @property
    def host(self) -> str:
        """获取 PostgreSQL 主机。"""
        return os.environ["PGVECTOR_HOST"]

    @property
    def port(self) -> int:
        """获取 PostgreSQL 端口。

        默认值：5432
        """
        return int(os.getenv("PGVECTOR_PORT", "5432"))

    @property
    def username(self) -> str:
        """获取 PostgreSQL 用户名。"""
        return os.environ["PGVECTOR_USER"]

    @property
    def password(self) -> str:
        """获取 PostgreSQL 密码。"""
        return os.environ["PGVECTOR_PASSWORD"]

    @property
    def database(self) -> str:
        """获取数据库名称。"""
        return os.environ["PGVECTOR_DATABASE"]

    @property
    def connect_timeout(self) -> int:
        """获取连接超时时间（秒）。

        默认值：30
        """
        return int(os.getenv("PGVECTOR_CONNECT_TIMEOUT", "30"))

    @property
    def sslmode(self) -> str:
        """获取连接的 SSL 模式。

        默认值：prefer
        有效选项：disable, allow, prefer, require, verify-ca, verify-full
        """
        return os.getenv("PGVECTOR_SSLMODE", "prefer")

    def get_client_config(self) -> dict:
        """获取 psycopg2/asyncpg 客户端的配置字典。

        返回：
            dict: 准备传递给 PostgreSQL 客户端的配置
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
        """验证所有必需的环境变量是否已设置。

        抛出：
            ValueError: 如果缺少任何必需的环境变量。
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
            raise ValueError(f"缺少必需的环境变量：{', '.join(missing_vars)}")


@dataclass
class MCPServerConfig:
    """MCP 服务器级设置配置。

    这些设置控制服务器传输和工具行为，
    与 ClickHouse 连接验证有意分离。

    可选的环境变量（带默认值）：
        MCP_SERVER_TRANSPORT: "stdio", "http", 或 "sse"（默认：stdio）
        MCP_BIND_HOST: HTTP/SSE 的绑定主机（默认：127.0.0.1）
        MCP_BIND_PORT: HTTP/SSE 的绑定端口（默认：8000）
        MCP_QUERY_TIMEOUT: SELECT 工具超时时间（秒）（默认：30）
    """

    @property
    def server_transport(self) -> str:
        transport = os.getenv("MCP_SERVER_TRANSPORT", TransportType.STDIO.value).lower()
        if transport not in TransportType.values():
            valid_options = ", ".join(f'"{t}"' for t in TransportType.values())
            raise ValueError(f"无效的传输类型 '{transport}'。有效选项：{valid_options}")
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

