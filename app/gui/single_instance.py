"""Single-instance coordination so the Finder Quick Actions reach an
already-open Describely window.

The macOS right-click verbs launch us with ``open -n -a Describely.app
--args --mode MODE FILE`` (the ``-n`` forces a brand-new process every
time — without it, ``open`` would merely re-activate the existing window
and the new ``--args`` would be silently dropped, which was the original
"file not added to queue" bug).

On startup each process tries to connect to a per-user local socket:

* Connect succeeds → a primary instance is already running. We are a
  transient *courier*: serialize ``(mode, files)``, hand them over, and
  exit. The primary enqueues them via its registered callback.
* Connect fails → we are the first instance. We bind the socket as the
  primary and keep listening for couriers for the rest of the session.

Best-effort and macOS-shaped but portable: the same mechanism works on
any platform Qt's QLocalSocket supports. Any IPC failure degrades to the
old behaviour (a second window's worth of work is lost) rather than
crashing — callers treat ``forward_to_primary`` returning False as "I am
the primary, carry on".
"""

from __future__ import annotations

import getpass
import hashlib
import os
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from app.gui.app_logger import log as _file_log

# Payload framing: newline-delimited UTF-8. First line is the mode value
# (matches JobMode's string values, e.g. "both" / "transcription"); the
# remaining non-empty lines are absolute file paths. Newlines never occur
# in our mode tokens and are stripped from paths below, so the framing is
# unambiguous for our inputs.
_ENCODING = "utf-8"
_CONNECT_TIMEOUT_MS = 500
_WRITE_TIMEOUT_MS = 1000
_READ_TIMEOUT_MS = 1000

# Forwarded payloads are tiny (a mode token + a handful of paths). Cap the
# read so a misbehaving / hostile client on the local socket can't make
# the primary buffer without bound.
_MAX_PAYLOAD_BYTES = 64 * 1024

# Callback invoked on the primary when a courier delivers files.
PayloadHandler = Callable[[str, list[Path]], None]


def _user_token() -> str:
    """Stable per-user identifier, portable across platforms.

    POSIX exposes a numeric uid via ``os.getuid``; Windows has no such
    call (the original macOS-first code crashed here with
    ``AttributeError: module 'os' has no attribute 'getuid'``). Fall back
    to the login name, then to the usual env vars, and hash the result so
    the socket name stays well-formed regardless of odd characters in a
    username.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        return str(getuid())

    name = (
        os.environ.get("USERNAME")
        or os.environ.get("USER")
        or _login_name()
        or "default"
    )
    return hashlib.sha1(name.encode(_ENCODING, errors="replace")).hexdigest()[:16]


def _login_name() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - never let name lookup crash startup
        return ""


def _server_name() -> str:
    """Per-user socket name so distinct logins don't collide.

    On macOS QLocalServer backs this with a socket file in a per-user
    temp dir; on Windows it maps to a named pipe. Scoping the name to the
    user is belt-and-suspenders for shared hosts and keeps the behaviour
    predictable across platforms.
    """
    return f"Describely-singleton-{_user_token()}"


def _encode_payload(mode: str, files: list[Path]) -> bytes:
    lines = [mode]
    lines.extend(str(p) for p in files)
    return ("\n".join(lines) + "\n").encode(_ENCODING)


def _decode_payload(raw: bytes) -> tuple[str, list[Path]]:
    text = raw.decode(_ENCODING, errors="replace")
    lines = [ln for ln in text.splitlines() if ln]
    if not lines:
        return "", []
    mode = lines[0]
    files = [Path(ln) for ln in lines[1:]]
    return mode, files


def forward_to_primary(mode: str, files: list[Path]) -> bool:
    """Try to hand ``(mode, files)`` to an already-running primary.

    Returns True if a primary was found and the payload was sent (the
    caller should then exit). Returns False if no primary is running —
    the caller is the primary and should call :func:`PrimaryServer.start`
    and continue normally.
    """
    socket = QLocalSocket()
    socket.connectToServer(_server_name())
    if not socket.waitForConnected(_CONNECT_TIMEOUT_MS):
        # No server (or it's not accepting): we are the primary.
        _file_log("single_instance: no primary found; this process is primary")
        return False  # not forwarded — caller is the primary

    payload = _encode_payload(mode, files)
    socket.write(payload)
    if not socket.waitForBytesWritten(_WRITE_TIMEOUT_MS):
        _file_log("single_instance: write to primary timed out", level="ERROR")
        socket.abort()
        # Couldn't deliver — fall back to running as our own window so the
        # user's action isn't lost entirely.
        return False
    socket.flush()
    # Wait for the primary to drain before we tear the socket down, so the
    # bytes aren't discarded by an early disconnect.
    socket.waitForBytesWritten(_WRITE_TIMEOUT_MS)
    socket.disconnectFromServer()
    if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        socket.waitForDisconnected(_WRITE_TIMEOUT_MS)
    _file_log(f"single_instance: forwarded {len(files)} file(s) to primary; mode={mode}")
    return True


class PrimaryServer(QObject):
    """Listens for couriers and forwards their payloads to a handler.

    Owned by the primary process for its whole lifetime. Stale socket
    files (left by a crashed previous primary) are cleared before
    listening so a fresh start isn't blocked.
    """

    def __init__(self, handler: PayloadHandler) -> None:
        super().__init__()
        self._handler = handler
        self._server: Optional[QLocalServer] = None
        # Per-connection read buffers, keyed by the socket object.
        self._buffers: dict[QLocalSocket, bytearray] = {}

    def start(self) -> bool:
        server = QLocalServer(self)
        # If a previous primary crashed, a stale socket file can linger
        # and make listen() fail with AddressInUseError. Clearing it is
        # safe here because forward_to_primary already proved nobody is
        # accepting connections on this name.
        QLocalServer.removeServer(_server_name())
        if not server.listen(_server_name()):
            _file_log(
                f"single_instance: listen failed: {server.errorString()}",
                level="ERROR",
            )
            return False
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        _file_log("single_instance: primary server listening")
        return True

    def _on_new_connection(self) -> None:
        assert self._server is not None
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(lambda s=socket: self._on_ready_read(s))
            socket.disconnected.connect(lambda s=socket: self._on_disconnected(s))

    def _on_ready_read(self, socket: QLocalSocket) -> None:
        buf = self._buffers.get(socket)
        if buf is None:
            return
        buf.extend(bytes(socket.readAll()))
        if len(buf) > _MAX_PAYLOAD_BYTES:
            _file_log("single_instance: payload too large; dropping", level="ERROR")
            self._buffers.pop(socket, None)
            socket.abort()

    def _on_disconnected(self, socket: QLocalSocket) -> None:
        buf = self._buffers.pop(socket, None)
        socket.deleteLater()
        if not buf:
            return
        mode, files = _decode_payload(bytes(buf))
        existing = [p for p in files if p.exists() and p.is_file()]
        _file_log(
            f"single_instance: received {len(existing)}/{len(files)} file(s); mode={mode}"
        )
        if existing:
            try:
                self._handler(mode, existing)
            except Exception as exc:  # noqa: BLE001 - never let IPC crash the UI
                _file_log(f"single_instance: handler raised: {exc}", level="ERROR")
