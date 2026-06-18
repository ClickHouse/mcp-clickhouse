"""Environment configuration for the MCP ClickHouse server.

This module handles all environment variable configuration with sensible defaults
and type conversion.
"""

from dataclasses import dataclass
import os
from typing import Optional
from enum import Enum

import yaml


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

    Required environment variables (only when CLICKHOUSE_ENABLED=true):
        CLICKHOUSE_HOST: The hostname of the ClickHouse server
        CLICKHOUSE_USER: The username for authentication
        CLICKHOUSE_PASSWORD: The password for authentication

    Optional environment variables (with defaults):
        CLICKHOUSE_ROLE: The role to use for authentication (default: None)
        CLICKHOUSE_PORT: The port number (default: 8443 if secure=True, 8123 if secure=False)
        CLICKHOUSE_SECURE: Enable HTTPS (default: true)
        CLICKHOUSE_VERIFY: Verify SSL certificates (default: true)
        CLICKHOUSE_SERVER_HOST_NAME: Server hostname for SNI override and certificate validation (default: None)
        CLICKHOUSE_CONNECT_TIMEOUT: Connection timeout in seconds (default: 30)
        CLICKHOUSE_SEND_RECEIVE_TIMEOUT: Send/receive timeout in seconds (default: 300)
        CLICKHOUSE_DATABASE: Default database to use (default: None)
        CLICKHOUSE_PROXY_PATH: Path to be added to the host URL. For instance, for servers behind an HTTP proxy (default: None)
        CLICKHOUSE_ENABLED: Enable ClickHouse server (default: true)
        CLICKHOUSE_ALLOW_WRITE_ACCESS: Allow write operations (DDL and DML) (default: false)
        CLICKHOUSE_ALLOW_DROP: Allow destructive operations (DROP, TRUNCATE) when writes are also enabled (default: false)
        CLICKHOUSE_CONFIG_FILE: Path to a clickhouse-client config.yaml to read connection settings from as a fallback for unset env vars (default: None, file not read)
        CLICKHOUSE_CONNECTION: Name of an entry under connections_credentials in CLICKHOUSE_CONFIG_FILE to use; when unset, top-level keys are used (default: None)
    """

    def __init__(self):
        """Initialize the configuration from environment variables."""
        # Cache for parsed config-file settings, keyed by (path, connection_name) and
        # mapping to (mtime_ns, settings). Avoids re-parsing on every property access
        # while still re-reading when the file is edited (see _file_settings).
        self._file_settings_cache: dict = {}
        if self.enabled:
            self._validate_required_vars()

    @property
    def enabled(self) -> bool:
        """Get whether ClickHouse server is enabled.

        Default: True
        """
        return os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true"

    def _resolve(self, env_var: str, file_key: str):
        """Resolve a setting from the environment, falling back to the config file.

        Returns the env var value if set, otherwise the config-file value, otherwise
        None (absent from both).
        """
        if env_var in os.environ:
            return os.environ[env_var]
        return self._file_settings().get(file_key)

    @property
    def host(self) -> str:
        """Get the ClickHouse host (CLICKHOUSE_HOST env var, then the config file)."""
        value = self._resolve("CLICKHOUSE_HOST", "host")
        if value is None:
            raise KeyError("CLICKHOUSE_HOST")
        return value

    @property
    def port(self) -> int:
        """Get the ClickHouse port.

        Defaults to 8443 if secure=True, 8123 if secure=False.
        Can be overridden by CLICKHOUSE_PORT environment variable.
        """
        if "CLICKHOUSE_PORT" in os.environ:
            return int(os.environ["CLICKHOUSE_PORT"])
        return 8443 if self.secure else 8123

    @property
    def username(self) -> str:
        """Get the ClickHouse username (CLICKHOUSE_USER env var, then the config file)."""
        value = self._resolve("CLICKHOUSE_USER", "username")
        if value is None:
            raise KeyError("CLICKHOUSE_USER")
        return value

    @property
    def password(self) -> str:
        """Get the ClickHouse password (CLICKHOUSE_PASSWORD env var, then the config file)."""
        value = self._resolve("CLICKHOUSE_PASSWORD", "password")
        if value is None:
            raise KeyError("CLICKHOUSE_PASSWORD")
        return value

    @property
    def role(self) -> Optional[str]:
        """Get the ClickHouse role."""
        return os.getenv("CLICKHOUSE_ROLE")

    @property
    def database(self) -> Optional[str]:
        """Get the default database name (CLICKHOUSE_DATABASE env var, then the config file)."""
        return self._resolve("CLICKHOUSE_DATABASE", "database")

    @property
    def secure(self) -> bool:
        """Get whether HTTPS is enabled.

        Resolution order: CLICKHOUSE_SECURE env var, then the config file (if any).
        Default: True
        """
        if "CLICKHOUSE_SECURE" in os.environ:
            return os.environ["CLICKHOUSE_SECURE"].lower() == "true"
        file_secure = self._file_settings().get("secure")
        return True if file_secure is None else file_secure

    @property
    def verify(self) -> bool:
        """Get whether SSL certificate verification is enabled.

        Default: True
        """
        return os.getenv("CLICKHOUSE_VERIFY", "true").lower() == "true"

    @property
    def server_host_name(self) -> Optional[str]:
        """Get the server hostname for SNI override."""
        return os.getenv("CLICKHOUSE_SERVER_HOST_NAME")

    @property
    def connect_timeout(self) -> int:
        """Get the connection timeout in seconds.

        Default: 30
        """
        return int(os.getenv("CLICKHOUSE_CONNECT_TIMEOUT", "30"))

    @property
    def send_receive_timeout(self) -> int:
        """Get the send/receive timeout in seconds.

        Default: 300 (ClickHouse default)
        """
        return int(os.getenv("CLICKHOUSE_SEND_RECEIVE_TIMEOUT", "300"))

    @property
    def proxy_path(self) -> str:
        return os.getenv("CLICKHOUSE_PROXY_PATH")

    @property
    def allow_write_access(self) -> bool:
        """Get whether write operations (DDL and DML) are allowed.

        Default: False
        """
        return os.getenv("CLICKHOUSE_ALLOW_WRITE_ACCESS", "false").lower() == "true"

    @property
    def allow_drop(self) -> bool:
        """Get whether DROP operations (DROP TABLE, DROP DATABASE) are allowed.

        This setting provides an additional safety layer when write access is enabled.
        Even with CLICKHOUSE_ALLOW_WRITE_ACCESS=true, DROP operations require this flag.

        Default: False
        """
        return os.getenv("CLICKHOUSE_ALLOW_DROP", "false").lower() == "true"

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
            "client_name": "mcp_clickhouse",
        }

        # Add optional role if set
        if self.role:
            config.setdefault("settings", {})["role"] = self.role

        # Add optional database if set
        if self.database:
            config["database"] = self.database

        if self.proxy_path:
            config["proxy_path"] = self.proxy_path

        if self.server_host_name:
            config["server_host_name"] = self.server_host_name

        return config

    def _validate_required_vars(self) -> None:
        """Validate that all required connection settings are available.

        Each value may come from an environment variable or, as a fallback, from
        CLICKHOUSE_CONFIG_FILE. Reading the file here also surfaces parse/lookup
        errors (missing file, unknown connection name) eagerly at startup.

        Raises:
            ValueError: If any required setting is missing from both sources.
        """
        file_settings = self._file_settings()
        required = {
            "CLICKHOUSE_HOST": "host",
            "CLICKHOUSE_USER": "username",
            "CLICKHOUSE_PASSWORD": "password",
        }
        missing_vars = [
            env_var
            for env_var, file_key in required.items()
            if env_var not in os.environ and file_settings.get(file_key) is None
        ]

        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

    @staticmethod
    def _secure_to_bool(value) -> bool:
        """Coerce a config-file `secure` value to a bool.

        Accepts native YAML booleans as well as the integer/string forms used by
        clickhouse-client (e.g. `secure: 1`, `secure: "true"`).
        """
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _file_settings(self) -> dict:
        """Load connection settings from CLICKHOUSE_CONFIG_FILE, if configured.

        Returns a normalized dict with any of the keys
        {host, username, password, database, secure} that are present in the file.
        `port` is intentionally never returned (see class docstring). Returns an
        empty dict when CLICKHOUSE_CONFIG_FILE is unset (feature off).

        Raises:
            ValueError: If the file is missing/unparseable, or CLICKHOUSE_CONNECTION
                names a connection that does not exist in the file.
        """
        path = os.getenv("CLICKHOUSE_CONFIG_FILE")
        if not path:
            return {}

        connection_name = os.getenv("CLICKHOUSE_CONNECTION")
        cache_key = (path, connection_name)

        # Invalidate the cache when the file changes on disk, so edits are picked up
        # without a restart. Stat'ing on each access is cheap; only re-parse on change.
        try:
            mtime = os.stat(path).st_mtime_ns
        except FileNotFoundError as e:
            raise ValueError(f"CLICKHOUSE_CONFIG_FILE not found: {path}") from e
        except OSError as e:
            raise ValueError(f"Failed to read CLICKHOUSE_CONFIG_FILE '{path}': {e}") from e

        cached = self._file_settings_cache.get(cache_key)
        if cached is not None and cached[0] == mtime:
            return cached[1]

        # The os.stat above already validated existence/readability; here we only
        # guard against a parse error or a TOCTOU race (OSError covers FileNotFoundError).
        try:
            with open(path) as f:
                raw = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as e:
            raise ValueError(f"Failed to read CLICKHOUSE_CONFIG_FILE '{path}': {e}") from e

        if not isinstance(raw, dict):
            raise ValueError(
                f"CLICKHOUSE_CONFIG_FILE '{path}' does not contain a top-level mapping"
            )

        if connection_name:
            source = self._select_connection(raw, connection_name, path)
            host_keys = ("hostname", "host")
        else:
            source = raw
            host_keys = ("host",)

        settings = {}
        for host_key in host_keys:
            if source.get(host_key) is not None:
                settings["host"] = source[host_key]
                break
        if source.get("user") is not None:
            settings["username"] = source["user"]
        if source.get("password") is not None:
            settings["password"] = source["password"]
        if source.get("database") is not None:
            settings["database"] = source["database"]
        if source.get("secure") is not None:
            settings["secure"] = self._secure_to_bool(source["secure"])

        self._file_settings_cache[cache_key] = (mtime, settings)
        return settings

    @staticmethod
    def _select_connection(raw: dict, connection_name: str, path: str) -> dict:
        """Find a named entry in the connections_credentials section of the file."""
        credentials = raw.get("connections_credentials") or {}
        connections = credentials.get("connection") if isinstance(credentials, dict) else None
        # `connection` may be a single mapping or a list of mappings.
        if isinstance(connections, dict):
            connections = [connections]
        elif not isinstance(connections, list):
            connections = []

        for entry in connections:
            if isinstance(entry, dict) and entry.get("name") == connection_name:
                return entry

        raise ValueError(
            f"CLICKHOUSE_CONNECTION '{connection_name}' not found in "
            f"connections_credentials of '{path}'"
        )


@dataclass
class ChDBConfig:
    """Configuration for chDB connection settings.

    This class handles all environment variable configuration with sensible defaults
    and type conversion. It provides typed methods for accessing each configuration value.

    Required environment variables:
        CHDB_DATA_PATH: The path to the chDB data directory (only required if CHDB_ENABLED=true)
    """

    def __init__(self):
        """Initialize the configuration from environment variables."""
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
        """Get the chDB data path."""
        return os.getenv("CHDB_DATA_PATH", ":memory:")

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


# Global instance placeholders for the singleton pattern
_CONFIG_INSTANCE = None
_CHDB_CONFIG_INSTANCE = None


def get_config():
    """
    Gets the singleton instance of ClickHouseConfig.
    Instantiates it on the first call.
    """
    global _CONFIG_INSTANCE
    if _CONFIG_INSTANCE is None:
        # Instantiate the config object here, ensuring load_dotenv() has likely run
        _CONFIG_INSTANCE = ClickHouseConfig()
    return _CONFIG_INSTANCE


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


@dataclass
class MCPServerConfig:
    """Configuration for MCP server-level settings.

    These settings control the server transport and tool behavior and are
    intentionally independent of ClickHouse connection validation.

    Optional environment variables (with defaults):
        CLICKHOUSE_MCP_SERVER_TRANSPORT: "stdio", "http", or "sse" (default: stdio)
        CLICKHOUSE_MCP_BIND_HOST: Bind host for HTTP/SSE (default: 127.0.0.1)
        CLICKHOUSE_MCP_BIND_PORT: Bind port for HTTP/SSE (default: 8000)
        CLICKHOUSE_MCP_QUERY_TIMEOUT: SELECT tool timeout in seconds (default: 30)
        CLICKHOUSE_MCP_AUTH_TOKEN: Static bearer token for HTTP/SSE transports.
            One authentication mode must be configured for HTTP/SSE; the other two
            options are FASTMCP_SERVER_AUTH (FastMCP OAuth/OIDC providers) and
            CLICKHOUSE_MCP_AUTH_DISABLED=true.
        CLICKHOUSE_MCP_AUTH_DISABLED: Disable authentication entirely (default: false,
            development only)
    """

    @property
    def server_transport(self) -> str:
        transport = os.getenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", TransportType.STDIO.value).lower()
        if transport not in TransportType.values():
            valid_options = ", ".join(f'"{t}"' for t in TransportType.values())
            raise ValueError(f"Invalid transport '{transport}'. Valid options: {valid_options}")
        return transport

    @property
    def bind_host(self) -> str:
        return os.getenv("CLICKHOUSE_MCP_BIND_HOST", "127.0.0.1")

    @property
    def bind_port(self) -> int:
        return int(os.getenv("CLICKHOUSE_MCP_BIND_PORT", "8000"))

    @property
    def query_timeout(self) -> int:
        return int(os.getenv("CLICKHOUSE_MCP_QUERY_TIMEOUT", "30"))

    @property
    def auth_token(self) -> Optional[str]:
        """Get the authentication token for HTTP/SSE transports."""
        return os.getenv("CLICKHOUSE_MCP_AUTH_TOKEN", None)

    @property
    def auth_disabled(self) -> bool:
        """Get whether authentication is disabled."""
        return os.getenv("CLICKHOUSE_MCP_AUTH_DISABLED", "false").lower() == "true"


_MCP_CONFIG_INSTANCE = None


def get_mcp_config() -> MCPServerConfig:
    """Gets the singleton instance of MCPServerConfig."""
    global _MCP_CONFIG_INSTANCE
    if _MCP_CONFIG_INSTANCE is None:
        _MCP_CONFIG_INSTANCE = MCPServerConfig()
    return _MCP_CONFIG_INSTANCE
