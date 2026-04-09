from __future__ import annotations

import os
import socket
from typing import Any

_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_IS_ENFORCED = False

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_allowed_host(host: str) -> bool:
    lowered = host.lower()
    if lowered in _ALLOWED_HOSTS:
        return True
    if lowered.startswith("127."):
        return True
    return False


def _extract_host(address: Any) -> str | None:
    if isinstance(address, tuple) and address:
        host = address[0]
        return str(host) if host is not None else None
    if isinstance(address, str):
        return address
    return None


def _block_if_external(host: str | None) -> None:
    if not host:
        return
    if _is_allowed_host(host):
        return
    raise OSError(
        "Network isolation is enabled: external internet access is blocked. "
        "Only localhost/127.0.0.1 connections are allowed."
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


def enforce_offline_mode() -> None:
    global _IS_ENFORCED
    if _IS_ENFORCED:
        return

    # Force common ML tooling into offline mode.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    socket.create_connection = _guarded_create_connection
    socket.socket.connect = _guarded_socket_connect
    socket.socket.connect_ex = _guarded_socket_connect_ex

    _IS_ENFORCED = True
