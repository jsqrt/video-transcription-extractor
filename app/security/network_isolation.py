"""Strict network isolation: block outbound connections to non-local hosts.

This module monkey-patches the ``socket`` layer so that any attempt to
connect to a hostname/IP that is not explicitly allow-listed raises
``OSError``. Hostname resolution (``getaddrinfo``/``gethostbyname``) is
also blocked for non-allow-listed names to prevent DNS leaks.

The set of allowed hosts is intentionally small: loopback only. Callers
that legitimately need to reach other hosts (for example an Ollama
sidecar on 127.0.0.1 or in a Docker network via 172.x) should extend
``add_allowed_host`` before calling :func:`enforce_offline_mode`.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any, Iterable

_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_GETHOSTBYNAME = socket.gethostbyname
_ORIGINAL_GETHOSTBYNAME_EX = socket.gethostbyname_ex
_IS_ENFORCED = False

_ALLOWED_NAMES: set[str] = {"localhost", "ip6-localhost", "ip6-loopback"}
_ALLOWED_NETWORKS: list[ipaddress._BaseNetwork] = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]


def add_allowed_host(host: str) -> None:
    """Extend the allow-list. Accepts hostnames, IPs, or CIDR blocks."""
    host_stripped = host.strip().lower()
    if not host_stripped:
        return
    try:
        network = ipaddress.ip_network(host_stripped, strict=False)
        _ALLOWED_NETWORKS.append(network)
        return
    except ValueError:
        pass
    _ALLOWED_NAMES.add(host_stripped)


def _is_allowed_host(host: str) -> bool:
    lowered = host.lower()
    if lowered in _ALLOWED_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return any(ip in network for network in _ALLOWED_NETWORKS)


def _extract_host(address: Any) -> str | None:
    if isinstance(address, tuple) and address:
        host = address[0]
        return str(host) if host is not None else None
    if isinstance(address, str):
        return address
    return None


class NetworkBlockedError(OSError):
    """Raised when network isolation blocks an outbound connection."""


def _block_if_external(host: str | None) -> None:
    if not host:
        return
    if _is_allowed_host(host):
        return
    raise NetworkBlockedError(
        f"Network isolation is enabled: outbound connection to '{host}' blocked. "
        "Only loopback hosts are allowed (127.0.0.0/8, ::1, localhost)."
    )


def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any):
    _block_if_external(_extract_host(address))
    return _ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)


def _guarded_socket_connect(self: socket.socket, address: Any):
    _block_if_external(_extract_host(address))
    return _ORIGINAL_SOCKET_CONNECT(self, address)


def _guarded_socket_connect_ex(self: socket.socket, address: Any):
    _block_if_external(_extract_host(address))
    return _ORIGINAL_SOCKET_CONNECT_EX(self, address)


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any):
    # Preserve None (meaning "any local address") and numeric passive flags.
    if host is None:
        return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)
    _block_if_external(str(host))
    return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)


def _guarded_gethostbyname(host: str):
    _block_if_external(host)
    return _ORIGINAL_GETHOSTBYNAME(host)


def _guarded_gethostbyname_ex(host: str):
    _block_if_external(host)
    return _ORIGINAL_GETHOSTBYNAME_EX(host)


def _apply_env_defaults() -> None:
    # Force common ML tooling into offline mode.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
    # Stop faster-whisper from checking for updates or pinging telemetry.
    os.environ.setdefault("CT2_VERBOSE", "0")


def enforce_offline_mode(extra_allowed_hosts: Iterable[str] | None = None) -> None:
    """Install the socket patches. Idempotent."""
    global _IS_ENFORCED
    if _IS_ENFORCED:
        return

    if extra_allowed_hosts:
        for host in extra_allowed_hosts:
            add_allowed_host(host)

    _apply_env_defaults()

    socket.create_connection = _guarded_create_connection
    socket.socket.connect = _guarded_socket_connect
    socket.socket.connect_ex = _guarded_socket_connect_ex
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.gethostbyname = _guarded_gethostbyname
    socket.gethostbyname_ex = _guarded_gethostbyname_ex

    _IS_ENFORCED = True


def is_enforced() -> bool:
    return _IS_ENFORCED


def _reset_for_tests() -> None:
    """Restore originals. Tests only — do not call from production code."""
    global _IS_ENFORCED
    socket.create_connection = _ORIGINAL_CREATE_CONNECTION
    socket.socket.connect = _ORIGINAL_SOCKET_CONNECT
    socket.socket.connect_ex = _ORIGINAL_SOCKET_CONNECT_EX
    socket.getaddrinfo = _ORIGINAL_GETADDRINFO
    socket.gethostbyname = _ORIGINAL_GETHOSTBYNAME
    socket.gethostbyname_ex = _ORIGINAL_GETHOSTBYNAME_EX
    _IS_ENFORCED = False
