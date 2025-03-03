"""Environment configuration for the MCP ClickHouse server.

This module handles all environment variable configuration with sensible defaults
and type conversion.
"""

from dataclasses import dataclass
import os
from typing import Optional


@dataclass
class ClickHouseConfig:
    """Configuration for ClickHouse connection settings.

    This class handles all environment variable configuration with sensible defaults
    and type conversion. It provides typed methods for accessing each configuration value.

    Default values (if environment variables are not set):
        CLICKHOUSE_HOST: "localhost"
        CLICKHOUSE_USER: ""
        CLICKHOUSE_PASSWORD: ""
        CLICKHOUSE_PORT: 8123
        CLICKHOUSE_SECURE: false
        CLICKHOUSE_VERIFY: false
        CLICKHOUSE_CONNECT_TIMEOUT: 5
        CLICKHOUSE_SEND_RECEIVE_TIMEOUT: 300
        CLICKHOUSE_DATABASE: None
    """

    def __init__(self):
        """Initialize the configuration from environment variables."""
        self._set_default_vars()

    @property
    def host(self) -> str:
        """Get the ClickHouse host."""
        return os.environ.get("CLICKHOUSE_HOST", "localhost")

    @property
    def port(self) -> int:
        """Get the ClickHouse port.

        Defaults to 8123 if not specified.
        """
        return int(os.environ.get("CLICKHOUSE_PORT", "8123"))

    @property
    def username(self) -> str:
        """Get the ClickHouse username."""
        return os.environ.get("CLICKHOUSE_USER", "")

    @property
    def password(self) -> str:
        """Get the ClickHouse password."""
        return os.environ.get("CLICKHOUSE_PASSWORD", "")

    @property
    def database(self) -> Optional[str]:
        """Get the default database name if set."""
        return os.getenv("CLICKHOUSE_DATABASE")

    @property
    def secure(self) -> bool:
        """Get whether HTTPS is enabled.

        Default: False
        """
        return os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true"

    @property
    def verify(self) -> bool:
        """Get whether SSL certificate verification is enabled.

        Default: False
        """
        return os.getenv("CLICKHOUSE_VERIFY", "false").lower() == "true"

    @property
    def connect_timeout(self) -> int:
        """Get the connection timeout in seconds.

        Default: 5
        """
        return int(os.getenv("CLICKHOUSE_CONNECT_TIMEOUT", "5"))

    @property
    def send_receive_timeout(self) -> int:
        """Get the send/receive timeout in seconds.

        Default: 300 (ClickHouse default)
        """
        return int(os.getenv("CLICKHOUSE_SEND_RECEIVE_TIMEOUT", "300"))

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
        }

        # Add optional database if set
        if self.database:
            config["database"] = self.database

        return config

    def _set_default_vars(self) -> None:
        """Set default values for environment variables if they are not already set."""
        defaults = {
            "CLICKHOUSE_HOST": "localhost",
            "CLICKHOUSE_USER": "",
            "CLICKHOUSE_PASSWORD": "",
            "CLICKHOUSE_PORT": "8123",
            "CLICKHOUSE_SECURE": "false",
            "CLICKHOUSE_VERIFY": "false",
            "CLICKHOUSE_CONNECT_TIMEOUT": "5",
            "CLICKHOUSE_SEND_RECEIVE_TIMEOUT": "300",
        }

        for var, default_value in defaults.items():
            if var not in os.environ:
                os.environ[var] = default_value


# Global instance for easy access
config = ClickHouseConfig()
