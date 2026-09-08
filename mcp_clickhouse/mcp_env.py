"""Environment configuration for the MCP ClickHouse server.

This module handles all environment variable configuration with sensible defaults
and type conversion.
"""

from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_network
import os
from typing import List, Optional
from enum import Enum
from urllib.parse import parse_qs, urlparse


_IPV4_MAPPED_IPV6 = ip_network("::ffff:0:0/96")
_TLS_MODES = frozenset({"mutual", "proxy", "strict"})
TLS_TOP_LEVEL_ONLY_KEYS = frozenset(
    {
        "verify",
        "ca_cert",
        "client_cert",
        "client_cert_key",
        "tls_mode",
        "server_host_name",
        "pool_mgr",
    }
)


def _normalize_tls_mode(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("CLICKHOUSE_TLS_MODE must be one of: mutual, proxy, strict")
    value = value.strip().lower()
    if not value:
        return None
    if value not in _TLS_MODES:
        raise ValueError("CLICKHOUSE_TLS_MODE must be one of: mutual, proxy, strict")
    return value


def normalize_secure_flag(value: object) -> bool:
    """Return the ClickHouse secure flag as a bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ValueError("CLICKHOUSE_SECURE must be true or false")


def _normalize_verify_flag(value: object) -> bool | str:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        if normalized == "proxy":
            return normalized
    raise ValueError("CLICKHOUSE_VERIFY must be true, false, or proxy")


def _effective_tls_mode(client_config: dict) -> Optional[str]:
    tls_mode = _normalize_tls_mode(client_config.get("tls_mode"))
    if tls_mode is not None:
        return tls_mode
    verify = client_config.get("verify")
    if verify is not None and _normalize_verify_flag(verify) == "proxy":
        return "proxy"
    return None


def _effective_interface(client_config: dict, secure: bool) -> str:
    interface = client_config.get("interface")
    if interface:
        return interface
    if secure or str(client_config.get("port")) in {"443", "8443"}:
        return "https"
    return "http"


def _dsn_tls_query_keys(dsn: object) -> list[str]:
    if not isinstance(dsn, str) or not dsn:
        return []
    try:
        query = urlparse(dsn).query
    except ValueError as exc:
        raise ValueError("ClickHouse DSN override is not a valid URL") from exc
    return sorted(TLS_TOP_LEVEL_ONLY_KEYS.intersection(parse_qs(query)))


def uses_mutual_tls_auth(client_config: dict) -> bool:
    """Return whether a client config selects ClickHouse certificate authentication."""
    return bool(
        client_config.get("client_cert") and _effective_tls_mode(client_config) in (None, "mutual")
    )


def validate_client_tls_config(client_config: dict) -> None:
    """Validate and normalize ClickHouse TLS client settings."""
    tls_mode = _normalize_tls_mode(client_config.get("tls_mode"))
    if tls_mode is None:
        client_config.pop("tls_mode", None)
    else:
        client_config["tls_mode"] = tls_mode

    secure = False
    if "secure" in client_config:
        secure = normalize_secure_flag(client_config["secure"])
        client_config["secure"] = secure

    verify = None
    if "verify" in client_config:
        verify = _normalize_verify_flag(client_config["verify"])
        client_config["verify"] = verify

    dsn = client_config.get("dsn")
    if isinstance(dsn, str) and dsn.startswith("chdb:"):
        raise ValueError("ClickHouse DSN overrides cannot select the chdb backend")
    dsn_tls_keys = _dsn_tls_query_keys(dsn)
    if dsn_tls_keys:
        raise ValueError(
            "ClickHouse DSN query parameters cannot set managed TLS keys: "
            f"{', '.join(dsn_tls_keys)}"
        )

    tls_options = {
        "CLICKHOUSE_CA_CERT": client_config.get("ca_cert"),
        "CLICKHOUSE_CLIENT_CERT": client_config.get("client_cert"),
        "CLICKHOUSE_CLIENT_CERT_KEY": client_config.get("client_cert_key"),
        "CLICKHOUSE_TLS_MODE": tls_mode,
    }
    configured_tls_options = ", ".join(name for name, value in tls_options.items() if value)
    if configured_tls_options and not secure:
        raise ValueError(f"{configured_tls_options} can only be used with CLICKHOUSE_SECURE=true")
    if configured_tls_options and _effective_interface(client_config, secure) != "https":
        raise ValueError(f"{configured_tls_options} can only be used with interface=https")
    if configured_tls_options and client_config.get("pool_mgr") is not None:
        raise ValueError(f"pool_mgr cannot be combined with {configured_tls_options}")

    if client_config.get("client_cert_key") and not client_config.get("client_cert"):
        raise ValueError("CLICKHOUSE_CLIENT_CERT_KEY requires CLICKHOUSE_CLIENT_CERT")
    if tls_mode and not client_config.get("client_cert"):
        raise ValueError("CLICKHOUSE_TLS_MODE requires CLICKHOUSE_CLIENT_CERT")
    if client_config.get("ca_cert") and verify not in (True, "proxy"):
        raise ValueError("CLICKHOUSE_CA_CERT requires CLICKHOUSE_VERIFY=true")

    if uses_mutual_tls_auth(client_config):
        client_config["password"] = ""


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
        CLICKHOUSE_PASSWORD: The password for authentication, except with mutual TLS

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
        CLICKHOUSE_CA_CERT: Path to CA certificate file for SSL verification (default: None)
        CLICKHOUSE_CLIENT_CERT: Path to client certificate file for mTLS authentication (default: None)
        CLICKHOUSE_CLIENT_CERT_KEY: Path to client private key file for mTLS authentication (default: None)
        CLICKHOUSE_TLS_MODE: TLS mode for client certificate usage - "mutual", "proxy", or "strict" (default: None)
    """

    def __init__(self):
        """Initialize the configuration from environment variables."""
        if self.enabled:
            self._validate_required_vars()

    @property
    def enabled(self) -> bool:
        """Get whether ClickHouse server is enabled.

        Default: True
        """
        return os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true"

    @property
    def host(self) -> str:
        """Get the ClickHouse host."""
        return os.environ["CLICKHOUSE_HOST"]

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
        """Get the ClickHouse username."""
        return os.environ["CLICKHOUSE_USER"]

    @property
    def password(self) -> str:
        """Get the ClickHouse password."""
        return os.getenv("CLICKHOUSE_PASSWORD", "")

    @property
    def role(self) -> Optional[str]:
        """Get the ClickHouse role."""
        return os.getenv("CLICKHOUSE_ROLE")

    @property
    def database(self) -> Optional[str]:
        """Get the default database name if set."""
        return os.getenv("CLICKHOUSE_DATABASE")

    @property
    def secure(self) -> bool:
        """Get whether HTTPS is enabled.

        Default: True
        """
        return os.getenv("CLICKHOUSE_SECURE", "true").lower() == "true"

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

    @property
    def ca_cert(self) -> Optional[str]:
        """Get the path to CA certificate file for SSL verification.

        Default: None
        """
        return os.getenv("CLICKHOUSE_CA_CERT")

    @property
    def client_cert(self) -> Optional[str]:
        """Get the path to client certificate file for mTLS authentication.

        Default: None
        """
        return os.getenv("CLICKHOUSE_CLIENT_CERT")

    @property
    def client_cert_key(self) -> Optional[str]:
        """Get the path to client private key file for mTLS authentication.

        This is optional if the client_cert file contains both the certificate
        and the private key (e.g., a combined .pem file).

        Default: None
        """
        return os.getenv("CLICKHOUSE_CLIENT_CERT_KEY")

    @property
    def tls_mode(self) -> Optional[str]:
        """Get the TLS mode for client certificate usage.

        Valid values:
        - "mutual": Use client certificate authentication
        - "proxy": Present a client certificate to a TLS proxy and use Basic authentication
        - "strict": Present a client certificate and use Basic authentication

        Default: None (clickhouse-connect uses mutual when client_cert is set)
        """
        return _normalize_tls_mode(os.getenv("CLICKHOUSE_TLS_MODE"))

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

        # Add mTLS configuration if set
        if self.ca_cert:
            config["ca_cert"] = self.ca_cert

        if self.client_cert:
            config["client_cert"] = self.client_cert

        if self.client_cert_key:
            config["client_cert_key"] = self.client_cert_key

        if self.tls_mode:
            config["tls_mode"] = self.tls_mode

        validate_client_tls_config(config)

        return config

    def _validate_required_vars(self) -> None:
        """Validate that all required environment variables are set.

        Raises:
            ValueError: If any required environment variable is missing.
        """
        tls_config = {
            "secure": self.secure,
            "verify": self.verify,
            "ca_cert": self.ca_cert,
            "client_cert": self.client_cert,
            "client_cert_key": self.client_cert_key,
            "tls_mode": self.tls_mode,
        }
        validate_client_tls_config(tls_config)

        missing_vars = []
        required_vars = ["CLICKHOUSE_HOST", "CLICKHOUSE_USER"]
        if not uses_mutual_tls_auth(tls_config):
            required_vars.append("CLICKHOUSE_PASSWORD")
        for var in required_vars:
            if var not in os.environ:
                missing_vars.append(var)

        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")


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


def _split_env_list(name: str) -> List[str]:
    """Read a comma separated environment variable as a list, ignoring blanks."""
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_WILDCARD_BIND_HOSTS = frozenset({"0.0.0.0", "::", "[::]"})


def _format_http_host(host: str, port: int) -> str:
    """Format a bind host as an HTTP Host header value."""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}"


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
        CLICKHOUSE_MCP_MAX_WORKERS: Maximum thread pool workers for query execution (default: 10)
        CLICKHOUSE_MCP_ALLOWED_HOSTS: Comma separated Host header values accepted on
            HTTP/SSE (default: derived from a concrete bind host and port)
        CLICKHOUSE_MCP_TRUSTED_PROXIES: Comma separated proxy IP addresses or CIDR
            networks whose X-Forwarded-* headers are trusted (default: none)
        CLICKHOUSE_MCP_ALLOWED_ORIGINS: Comma separated Origin header values accepted on
            HTTP/SSE (default: no browser origins are allowed)
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
    def max_workers(self) -> int:
        """Maximum thread pool workers for query execution.

        Default: 10
        """
        return int(os.getenv("CLICKHOUSE_MCP_MAX_WORKERS", "10"))

    @property
    def allowed_hosts(self) -> List[str]:
        """Host header values the HTTP/SSE transports answer for.

        A concrete bind address supplies a secure default. Loopback binds accept
        the IPv4, IPv6, and localhost aliases on any port. Wildcard binds require
        an explicit non-empty setting because the public Host value cannot be
        inferred. Entries may use a "localhost:*" any-port form.
        """
        name = "CLICKHOUSE_MCP_ALLOWED_HOSTS"
        if name in os.environ:
            allowed_hosts = _split_env_list(name)
            if not allowed_hosts:
                raise ValueError(f"{name} is set but contains no Host values")
            return allowed_hosts

        bind_host = self.bind_host.lower()
        if bind_host in _WILDCARD_BIND_HOSTS:
            raise ValueError(
                f"{name} must contain the public host name or address when "
                "CLICKHOUSE_MCP_BIND_HOST is a wildcard address"
            )

        if bind_host in _LOOPBACK_BIND_HOSTS:
            return [
                "127.0.0.1",
                "127.0.0.1:*",
                "localhost",
                "localhost:*",
                "[::1]",
                "[::1]:*",
            ]
        return [_format_http_host(self.bind_host, self.bind_port)]

    @property
    def trusted_proxies(self) -> List[IPv4Network | IPv6Network]:
        """Get proxy addresses allowed to supply X-Forwarded-* headers."""
        name = "CLICKHOUSE_MCP_TRUSTED_PROXIES"
        networks = []
        for value in _split_env_list(name):
            if value == "*":
                raise ValueError(f"{name} cannot trust every address")
            if "%" in value:
                raise ValueError(f"Scoped IPv6 addresses are not supported in {name}: {value}")
            try:
                network = ip_network(value)
            except ValueError as exc:
                raise ValueError(f"Invalid IP address or CIDR in {name}: {value}") from exc
            if isinstance(network, IPv6Network) and network.subnet_of(_IPV4_MAPPED_IPV6):
                # Normalize IPv4-mapped IPv6 to IPv4 so dual-stack peers match
                # and ::ffff:0:0/96 hits the trust-all rejection below.
                network = ip_network(
                    f"{network.network_address.ipv4_mapped}/{network.prefixlen - 96}"
                )
            if network.prefixlen == 0:
                raise ValueError(f"{name} cannot trust every address")
            if isinstance(network, IPv6Network) and network.overlaps(_IPV4_MAPPED_IPV6):
                # A supernet of the mapped range would trust every IPv4 peer on
                # a dual-stack bind, equivalent to the rejected 0.0.0.0/0.
                raise ValueError(
                    f"{name} entries cannot contain the whole IPv4-mapped range "
                    f"::ffff:0:0/96: {value}"
                )
            networks.append(network)
        return networks

    @property
    def allowed_origins(self) -> List[str]:
        """Origin header values the HTTP/SSE transports accept.

        Requests without an Origin header are always accepted. An empty list
        rejects every request that carries an Origin header.
        """
        return _split_env_list("CLICKHOUSE_MCP_ALLOWED_ORIGINS")

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
