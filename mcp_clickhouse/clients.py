import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import clickhouse_connect
from clickhouse_connect.driver.exceptions import OperationalError
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context

from mcp_clickhouse.mcp_env import (
    TLS_TOP_LEVEL_ONLY_KEYS,
    get_config,
    get_mcp_config,
    normalize_secure_flag,
    uses_mutual_tls_auth,
    validate_client_tls_config,
)

logger = logging.getLogger("mcp-clickhouse")

CLIENT_CONFIG_OVERRIDES_KEY = "clickhouse_client_config_overrides"
_CLIENT_CONFIG_OVERRIDES_UNSET = object()
_NESTED_CLIENT_CONFIG_KEYS = ("settings", "generic_args")
_REJECTED_ROLE_OVERRIDE_KEYS = ("role", "ch_role")
_CLIENT_CACHE_MAXSIZE = 64
_CLIENT_IDLE_PING_THRESHOLD = 60

# ClickHouse native TCP protocol ports (clickhouse-client). This MCP server uses the
# HTTP interface only (default 8123 / 8443). Connecting to native ports fails with
# messages like "Port 9000 is for clickhouse-client program".
_NATIVE_PROTOCOL_PORTS = frozenset({9000, 9440})

# Privileges the drop gate pretends to block but cannot enforce server-side.
# ALTER includes ALTER DELETE and ALTER DROP PARTITION in the privilege
# hierarchy. ALTER ADD is exempt because the README recipe grants it.
_GRANTS_ADVISORY_PRIVILEGES = re.compile(
    r"\b(ALL|DROP|TRUNCATE|DELETE|UPDATE|ALTER\b(?!\s+ADD\b))\b", re.IGNORECASE
)


@dataclass
class _ClientCacheEntry:
    client: Any
    last_used: float
    active_users: int = 0
    retired: bool = False
    closed: bool = False


def _is_connection_error(err: Exception) -> bool:
    """Check if an exception indicates a broken connection rather than a query error."""
    if isinstance(err, (OSError, ConnectionError, OperationalError)):
        return True
    err_str = str(err).lower()
    return any(s in err_str for s in ("connection", "timed out", "reset by peer", "eof"))


def _freeze_client_config_value(value: Any) -> Optional[tuple]:
    """Convert a client config value into a stable cache-key component."""
    if isinstance(value, Mapping):
        frozen_items = []
        try:
            items = sorted(value.items())
        except TypeError:
            return None
        for key, nested_value in items:
            frozen_value = _freeze_client_config_value(nested_value)
            if frozen_value is None:
                return None
            frozen_items.append((key, frozen_value))
        return ("mapping", tuple(frozen_items))
    if isinstance(value, list):
        frozen_items = tuple(_freeze_client_config_value(item) for item in value)
        if any(item is None for item in frozen_items):
            return None
        return ("list", frozen_items)
    if isinstance(value, tuple):
        frozen_items = tuple(_freeze_client_config_value(item) for item in value)
        if any(item is None for item in frozen_items):
            return None
        return ("tuple", frozen_items)
    if isinstance(value, (set, frozenset)):
        frozen_items = tuple(_freeze_client_config_value(item) for item in value)
        if any(item is None for item in frozen_items):
            return None
        return ("set", frozenset(frozen_items))
    try:
        hash(value)
    except TypeError:
        return None
    return ("value", value)


def _config_to_cache_key(config: dict) -> Optional[tuple]:
    """Convert a client config dict into a stable cache key when possible."""
    frozen_config = []
    for key, value in sorted(config.items()):
        frozen_value = _freeze_client_config_value(value)
        if frozen_value is None:
            return None
        frozen_config.append((key, frozen_value))
    return tuple(frozen_config)


def _connection_error_hints(error: Exception, client_config: dict) -> List[str]:
    """Return actionable hints for common ClickHouse connection misconfigurations.

    Helps users who confuse MCP transport settings with database settings, or who
    point CLICKHOUSE_PORT at the native TCP protocol instead of the HTTP interface.
    """
    hints: List[str] = []
    err = str(error).lower()
    port = client_config.get("port")
    secure = bool(client_config.get("secure"))
    host = client_config.get("host", "<unknown>")

    native_response_port = next(
        (
            native_port
            for native_port in _NATIVE_PROTOCOL_PORTS
            if f"port {native_port} is for clickhouse-client" in err
        ),
        None,
    )
    if port in _NATIVE_PROTOCOL_PORTS:
        hints.append(
            f"CLICKHOUSE_PORT={port} looks like ClickHouse's native TCP protocol port "
            "(used by clickhouse-client). This server uses the HTTP interface — set "
            "CLICKHOUSE_PORT to 8123 (HTTP) or 8443 (HTTPS), or your deployment's HTTP "
            "mapping. Do not use native ports 9000/9440."
        )
    elif native_response_port is not None:
        hints.append(
            f"The ClickHouse response indicates that this request reached native TCP port "
            f"{native_response_port}, even though the client was configured for {host}:{port}. "
            "Check DNS, service, proxy, load-balancer, and port mappings to ensure traffic is "
            "routed to ClickHouse's HTTP interface (8123/8443 by default, or your deployment's "
            "HTTP mapping)."
        )

    tls_tokens = (
        "ssl",
        "tls",
        "certificate",
        "handshake",
        "wrong version number",
        "certificate verify failed",
        "unexpected_eof",
        "eof occurred in violation of protocol",
    )
    if any(token in err for token in tls_tokens):
        scheme = "HTTPS" if secure else "HTTP"
        hints.append(
            f"TLS/SSL error while connecting with CLICKHOUSE_SECURE="
            f"{str(secure).lower()} ({scheme} to {host}:{port}). "
            "CLICKHOUSE_SECURE enables HTTPS for the ClickHouse database connection "
            "only — it is not MCP or ingress TLS. Use true for HTTPS database "
            "endpoints (ClickHouse Cloud / port 8443) and false only for plain HTTP "
            "(typical local Docker on 8123)."
        )

    # General connectivity and scheme/port failures can surface as opaque HTTP errors.
    connection_failure_tokens = (
        "http status",
        "bad status line",
        "connection refused",
        "connection reset",
        "remote end closed connection",
    )
    if any(token in err for token in connection_failure_tokens) and not hints:
        hints.append(
            f"Connection to {host}:{port} failed. Verify ClickHouse is running and reachable "
            "at this address and that network or proxy routing permits access. Then confirm "
            f"CLICKHOUSE_SECURE={str(secure).lower()} matches whether ClickHouse expects HTTPS, "
            "and that CLICKHOUSE_PORT is an HTTP interface port (8123/8443), not a native TCP "
            "port (9000/9440). These settings configure the database client, not the MCP "
            "server transport."
        )

    return hints


def _format_connection_failure(error: Exception, client_config: dict) -> str:
    """Build a connection failure message with optional configuration hints."""
    message = f"Failed to connect to ClickHouse: {error}"
    hints = _connection_error_hints(error, client_config)
    if hints:
        message += "\n" + "\n".join(f"Hint: {hint}" for hint in hints)
    return message


def _snapshot_client_config_overrides(overrides: Any) -> Optional[dict[str, Any]]:
    """Validate and copy request-scoped ClickHouse client overrides."""
    if overrides is None:
        return None
    if not isinstance(overrides, dict):
        raise ToolError(f"{CLIENT_CONFIG_OVERRIDES_KEY} must be a dict")

    snapshot = dict(overrides)
    for key in _REJECTED_ROLE_OVERRIDE_KEYS:
        if key in snapshot:
            raise ToolError(
                f"{CLIENT_CONFIG_OVERRIDES_KEY}.{key} is not supported; "
                f"use {CLIENT_CONFIG_OVERRIDES_KEY}.settings.role"
            )
    for key in _NESTED_CLIENT_CONFIG_KEYS:
        if key not in snapshot:
            continue
        value = snapshot[key]
        if not isinstance(value, Mapping):
            raise ToolError(f"{CLIENT_CONFIG_OVERRIDES_KEY}.{key} must be a mapping")
        if key == "generic_args":
            rejected_tls_keys = sorted(TLS_TOP_LEVEL_ONLY_KEYS.intersection(value))
            if rejected_tls_keys:
                raise ToolError(
                    f"{CLIENT_CONFIG_OVERRIDES_KEY}.generic_args cannot set TLS client keys: "
                    f"{', '.join(rejected_tls_keys)}"
                )
            for role_key in _REJECTED_ROLE_OVERRIDE_KEYS:
                if role_key in value:
                    raise ToolError(
                        f"{CLIENT_CONFIG_OVERRIDES_KEY}.generic_args.{role_key} "
                        "is not supported; "
                        f"use {CLIENT_CONFIG_OVERRIDES_KEY}.settings.role"
                    )
        snapshot[key] = dict(value)
    return snapshot


def _request_client_config_overrides(ctx: Any) -> Optional[dict[str, Any]]:
    """Read request-local ClickHouse overrides from a FastMCP context."""
    request_state = getattr(ctx, "_request_state", None)
    make_state_key = getattr(ctx, "_make_state_key", None)
    if not isinstance(request_state, Mapping) or not callable(make_state_key):
        raise RuntimeError(
            "FastMCP request-local state API is unavailable; validate the FastMCP version"
        )
    overrides = request_state.get(make_state_key(CLIENT_CONFIG_OVERRIDES_KEY))
    return _snapshot_client_config_overrides(overrides)


def _get_client_config_overrides() -> Optional[dict[str, Any]]:
    """Capture ClickHouse client overrides from the active FastMCP request."""
    try:
        ctx = get_context()
    except RuntimeError:
        return None
    # FastMCP 4.0 get_state() falls back to session state. There is no public
    # request-only accessor. Validate this private adapter before a minor upgrade.
    return _request_client_config_overrides(ctx)


async def _get_client_config_overrides_for_tool() -> Optional[dict[str, Any]]:
    """Reject session-scoped overrides before an MCP-facing tool dispatch."""
    try:
        ctx = get_context()
    except RuntimeError:
        return None
    overrides = _request_client_config_overrides(ctx)
    if overrides is not None:
        return overrides
    session_value = await ctx.get_state(CLIENT_CONFIG_OVERRIDES_KEY)
    if session_value is not None:
        raise ToolError(
            f"{CLIENT_CONFIG_OVERRIDES_KEY} must be request-scoped; middleware must call "
            "Context.set_state(..., serializable=False)"
        )
    return None


def _apply_client_config_overrides(
    client_config: dict[str, Any], overrides: Optional[dict[str, Any]]
) -> None:
    """Merge request-scoped overrides into the base client configuration."""
    if overrides is None:
        return

    logger.debug(
        "Applying request-specific ClickHouse client config override keys: %s",
        list(overrides.keys()),
    )
    remaining_overrides = dict(overrides)
    for key in _NESTED_CLIENT_CONFIG_KEYS:
        if key not in remaining_overrides:
            continue
        base_value = client_config.get(key, {})
        if base_value is None:
            base_value = {}
        if not isinstance(base_value, Mapping):
            raise ToolError(f"Base ClickHouse client config {key} must be a mapping")
        client_config[key] = {**base_value, **remaining_overrides.pop(key)}
    client_config.update(remaining_overrides)


class _ResolvedClientConfig(dict):
    """Client config with request override provenance."""

    def __init__(self, config: dict[str, Any], overrides_applied: bool):
        super().__init__(config)
        self.overrides_applied = overrides_applied


def _resolve_client_config(
    client_config_overrides: Any = _CLIENT_CONFIG_OVERRIDES_UNSET,
) -> _ResolvedClientConfig:
    """Resolve a client config from environment settings and explicit overrides."""
    if client_config_overrides is _CLIENT_CONFIG_OVERRIDES_UNSET:
        overrides = _get_client_config_overrides()
    else:
        overrides = _snapshot_client_config_overrides(client_config_overrides)

    try:
        client_config = get_config().get_client_config()
        _apply_client_config_overrides(client_config, overrides)

        if overrides is not None:
            secure = normalize_secure_flag(client_config.get("secure"))
            client_config["secure"] = secure
            expected_interface = "https" if secure else "http"
            if "interface" in overrides:
                if client_config.get("interface") != expected_interface:
                    raise ValueError(
                        "ClickHouse client interface override must be http or https "
                        "and match the secure setting"
                    )
            elif "secure" in overrides:
                client_config["interface"] = expected_interface

        password_overridden = bool(overrides is not None and "password" in overrides)
        if (
            not uses_mutual_tls_auth(client_config)
            and not password_overridden
            and client_config.get("password") in (None, "")
            and "CLICKHOUSE_PASSWORD" in os.environ
        ):
            client_config["password"] = os.environ["CLICKHOUSE_PASSWORD"]
        validate_client_tls_config(client_config)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    timeout_overridden = bool(overrides and "send_receive_timeout" in overrides)
    if "CLICKHOUSE_SEND_RECEIVE_TIMEOUT" not in os.environ and not timeout_overridden:
        query_timeout = get_mcp_config().query_timeout
        effective_timeout = client_config.get("send_receive_timeout", 300)
        if effective_timeout > query_timeout + 5:
            client_config["send_receive_timeout"] = query_timeout + 5

    return _ResolvedClientConfig(
        client_config,
        overrides_applied=bool(overrides),
    )


def _close_client(client) -> None:
    """Close a ClickHouse client without masking the caller's result."""
    try:
        client.close()
    except Exception:
        logger.debug("Failed to close ClickHouse client", exc_info=True)


def _retire_client_entry_locked(entry: _ClientCacheEntry):
    """Retire an entry and return its client when it can be closed now."""
    entry.retired = True
    if entry.active_users == 0 and not entry.closed:
        entry.closed = True
        return entry.client
    return None


def _warn_for_native_protocol_port(config: dict) -> None:
    """Warn when the client is configured with a native protocol port."""
    port = config.get("port")
    if port in _NATIVE_PROTOCOL_PORTS:
        logger.warning(
            "CLICKHOUSE_PORT=%s is a native TCP protocol port (clickhouse-client). "
            "mcp-clickhouse uses the HTTP interface; prefer 8123 (HTTP) or 8443 (HTTPS).",
            port,
        )


def _create_uncached_clickhouse_client(config: dict, *, cache_owned: bool):
    """Create and validate a ClickHouse client outside the cache lock."""
    config_fields = [
        f"secure={config['secure']}",
        f"verify={config['verify']}",
        f"connect_timeout={config['connect_timeout']}s",
        f"send_receive_timeout={config['send_receive_timeout']}s",
    ]
    if "server_host_name" in config:
        config_fields.append(f"server_host_name={config['server_host_name']}")
    logger.info(
        f"Creating ClickHouse client connection to {config['host']}:{config['port']} "
        f"as {config['username']} "
        f"({', '.join(config_fields)})"
    )

    try:
        connection_config = dict(config)
        if cache_owned:
            connection_config["autogenerate_session_id"] = False
        client = clickhouse_connect.get_client(**connection_config)
        version = client.server_version
        logger.info(f"Successfully connected to ClickHouse server version {version}")
        return client
    except Exception as e:
        message = _format_connection_failure(e, config)
        logger.error(message)
        raise


def build_query_settings(client) -> dict[str, str]:
    """Build query settings dict for ClickHouse queries.

    Always returns a dict (possibly empty) to ensure consistent behavior.
    """
    readonly_setting = get_readonly_setting(client)
    if readonly_setting is not None:
        return {"readonly": readonly_setting}
    return {}


def get_readonly_setting(client) -> Optional[str]:
    """Determine the readonly setting value for queries.

    This implements the following logic:
    1. If CLICKHOUSE_ALLOW_WRITE_ACCESS=true (writes enabled):
       - Allow writes if server permits (server readonly=None or "0")
       - Fall back to server's readonly setting if server enforces it
       - Log a warning when falling back

    2. If CLICKHOUSE_ALLOW_WRITE_ACCESS=false (default, read-only mode):
       - Enforce readonly=1 if server allows writes
       - Respect server's readonly setting if server enforces stricter mode

    Returns:
        "0" = writes allowed
        "1" = read-only mode (allows SET of non-privileged settings)
        "2" = strict read-only (server enforced; disallows SET)
        None = use server default (shouldn't happen in practice)
    """
    config = get_config()
    server_settings = getattr(client, "server_settings", {}) or {}
    server_readonly = _normalize_readonly_value(server_settings.get("readonly"))

    # Case 1: User wants write access (CLICKHOUSE_ALLOW_WRITE_ACCESS=true)
    if config.allow_write_access:
        if server_readonly in (None, "0"):
            logger.info("Write mode enabled (CLICKHOUSE_ALLOW_WRITE_ACCESS=true)")
            return "0"

        # If server forbids writes, respect server configuration
        logger.warning(
            "CLICKHOUSE_ALLOW_WRITE_ACCESS=true but server enforces readonly=%s; "
            "write operations will fail",
            server_readonly,
        )
        return server_readonly

    # Case 2: User wants read-only mode (CLICKHOUSE_ALLOW_WRITE_ACCESS=false, default)
    if server_readonly in (None, "0"):
        return "1"  # Enforce read-only since server allows writes

    return server_readonly  # Server already enforces readonly, respect it


def _normalize_readonly_value(value: Any) -> Optional[str]:
    """Normalize ClickHouse readonly setting to a simple string.

    The clickhouse_connect library represents settings as objects with a .value attribute.
    This function extracts the actual value for our logic.

    Args:
        value: The readonly setting value from ClickHouse server. Can be:
            - None (server has no readonly restriction)
            - A clickhouse_connect setting object with a .value attribute
            - An int (0, 1, 2)
            - A str ("0", "1", "2")

    Returns:
        Optional[str]: Normalized readonly value as string ("0", "1", "2") or None
    """
    if value is None:
        return None

    # Extract value from clickhouse_connect setting object
    if hasattr(value, "value"):
        value = value.value

    return str(value)


class _ClickHouseClients:
    """Client cache and grants advisory owned by one server assembly."""

    def __init__(self):
        self.cache: OrderedDict[Tuple, _ClientCacheEntry] = OrderedDict()
        self.lock = threading.Lock()
        self.grants_advisory_done = False

    def _retain_client_entry(self, entry: Optional[_ClientCacheEntry]):
        """Retain an open client lease for cancellation."""
        with self.lock:
            if entry is None or entry.closed:
                return None
            entry.active_users += 1
            return entry.client

    def _warn_if_overprivileged(self, client) -> None:
        """Warn once when the drop gate is active but the ClickHouse user holds
        privileges it cannot enforce against. Fail-open, never raises.
        """
        if self.grants_advisory_done:
            return
        # Check-then-set race across executor threads is harmless, worst case is a
        # duplicate warning.
        self.grants_advisory_done = True

        try:
            result = client.query("SHOW GRANTS")
            matched: set[str] = set()
            role_grants = []
            for row in result.result_rows:
                grant = str(row[0])
                if grant.upper().startswith("GRANT") and not re.search(r"\bON\b", grant, re.IGNORECASE):
                    # `GRANT <role> TO ...`; role privileges are not expanded here.
                    role_grants.append(grant)
                    continue
                matched.update(m.group(1).upper() for m in _GRANTS_ADVISORY_PRIVILEGES.finditer(grant))
            if matched:
                logger.warning(
                    "CLICKHOUSE_ALLOW_DROP=false, but the ClickHouse user holds %s privileges. "
                    "The destructive-operation gate runs in the MCP server and is not enforced "
                    "server-side. See the README least-privilege recipe to restrict grants.",
                    ", ".join(sorted(matched)),
                )
            for grant in role_grants:
                logger.info(
                    "Grants advisory cannot inspect privileges granted via roles: %s", grant
                )
        except Exception as e:
            logger.debug("Grants advisory skipped: %s", e)

    def _release_client_entry(self, entry: _ClientCacheEntry) -> None:
        """Release a client lease and close a retired entry after its final user."""
        client_to_close = None
        with self.lock:
            if entry.active_users <= 0:
                raise RuntimeError("ClickHouse client cache entry released without a lease")
            entry.active_users -= 1
            if entry.retired and entry.active_users == 0 and not entry.closed:
                entry.closed = True
                client_to_close = entry.client
        if client_to_close is not None:
            _close_client(client_to_close)

    def _evict_lru_entries_locked(self) -> List[Any]:
        """Retire least recently used entries until the cache is within its bound."""
        clients_to_close = []
        while len(self.cache) > _CLIENT_CACHE_MAXSIZE:
            _, entry = self.cache.popitem(last=False)
            client_to_close = _retire_client_entry_locked(entry)
            if client_to_close is not None:
                clients_to_close.append(client_to_close)
        return clients_to_close

    def _evict_cached_client(self, config: dict, failed_client) -> bool:
        """Evict only the cached client instance that produced a connection error."""
        cache_key = _config_to_cache_key(config)
        if cache_key is None:
            return False
        client_to_close = None
        with self.lock:
            entry = self.cache.get(cache_key)
            if entry is None or entry.client is not failed_client:
                return False
            self.cache.pop(cache_key)
            client_to_close = _retire_client_entry_locked(entry)
        logger.info("Evicted stale cached ClickHouse client")
        if client_to_close is not None:
            _close_client(client_to_close)
        return True

    def _return_client(self, client, config: dict):
        """Run base-client checks before returning a cached or new client."""
        overrides_applied = getattr(config, "overrides_applied", False)
        server_config = get_config()
        if (
            not overrides_applied
            and server_config.allow_write_access
            and not server_config.allow_drop
        ):
            self._warn_if_overprivileged(client)
        return client

    def _prepare_client_entry(
        self,
        entry: _ClientCacheEntry, config: dict
    ) -> _ClientCacheEntry:
        """Run base-client checks while the caller holds a lease."""
        try:
            self._return_client(entry.client, config)
        except Exception:
            self._release_client_entry(entry)
            raise
        return entry

    def _acquire_clickhouse_client(self, config: dict) -> _ClientCacheEntry:
        """Acquire a leased cached client, creating one when needed."""
        _warn_for_native_protocol_port(config)
        cache_key = _config_to_cache_key(config)
        if cache_key is None:
            client = _create_uncached_clickhouse_client(config, cache_owned=True)
            entry = _ClientCacheEntry(
                client=client,
                last_used=time.time(),
                active_users=1,
                retired=True,
            )
            return self._prepare_client_entry(entry, config)

        candidate = None
        cached_entry = None
        with self.lock:
            entry = self.cache.get(cache_key)
            if entry is not None and not entry.retired and not entry.closed:
                entry.active_users += 1
                if time.time() - entry.last_used > _CLIENT_IDLE_PING_THRESHOLD:
                    candidate = entry
                else:
                    entry.last_used = time.time()
                    self.cache.move_to_end(cache_key)
                    cached_entry = entry
        if cached_entry is not None:
            logger.debug("Reusing cached client")
            return self._prepare_client_entry(cached_entry, config)

        if candidate is not None:
            try:
                alive = candidate.client.ping()
            except Exception:
                alive = False

            replacement = None
            with self.lock:
                current = self.cache.get(cache_key)
                if alive and current is candidate and not candidate.retired:
                    candidate.last_used = time.time()
                    self.cache.move_to_end(cache_key)
                else:
                    if current is candidate:
                        self.cache.pop(cache_key)
                    _retire_client_entry_locked(candidate)
                    if current is not None and current is not candidate and not current.retired:
                        current.active_users += 1
                        current.last_used = time.time()
                        self.cache.move_to_end(cache_key)
                        replacement = current

            if alive and replacement is None and not candidate.retired:
                logger.debug("Reusing cached client (ping OK after idle)")
                return self._prepare_client_entry(candidate, config)

            self._release_client_entry(candidate)
            if replacement is not None:
                logger.debug("Reusing cached client after concurrent replacement")
                return self._prepare_client_entry(replacement, config)
            if not alive:
                logger.warning("Cached client failed ping, creating new client")

        client = _create_uncached_clickhouse_client(config, cache_owned=True)
        new_entry = _ClientCacheEntry(client=client, last_used=time.time(), active_users=1)
        winner = new_entry
        clients_to_close = []
        with self.lock:
            current = self.cache.get(cache_key)
            if current is not None and not current.retired and not current.closed:
                current.active_users += 1
                current.last_used = time.time()
                self.cache.move_to_end(cache_key)
                winner = current
                clients_to_close.append(client)
            else:
                if current is not None:
                    self.cache.pop(cache_key)
                    client_to_close = _retire_client_entry_locked(current)
                    if client_to_close is not None:
                        clients_to_close.append(client_to_close)
                self.cache[cache_key] = new_entry
                clients_to_close.extend(self._evict_lru_entries_locked())

        for client_to_close in clients_to_close:
            _close_client(client_to_close)

        return self._prepare_client_entry(winner, config)

    def create_clickhouse_client(
        self,
        client_config_overrides: Any = _CLIENT_CONFIG_OVERRIDES_UNSET,
        *,
        config: Optional[dict] = None,
    ):
        """Create an independently owned ClickHouse client for the given config."""
        if config is None:
            config = _resolve_client_config(client_config_overrides)
        elif client_config_overrides is not _CLIENT_CONFIG_OVERRIDES_UNSET:
            raise TypeError("Pass client_config_overrides or config, not both")

        _warn_for_native_protocol_port(config)
        client = _create_uncached_clickhouse_client(config, cache_owned=False)
        try:
            return self._return_client(client, config)
        except Exception:
            _close_client(client)
            raise

    def _clear_client_cache(self):
        """Retire all cached clients, closing those without active users."""
        clients_to_close = []
        with self.lock:
            for entry in self.cache.values():
                client_to_close = _retire_client_entry_locked(entry)
                if client_to_close is not None:
                    clients_to_close.append(client_to_close)
            self.cache.clear()
        for client in clients_to_close:
            _close_client(client)
