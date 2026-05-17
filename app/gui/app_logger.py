"""File-based logger that survives ``--windowed`` builds.

PyInstaller's GUI bundles (built with ``console=False``) discard stdout
and stderr entirely. Without persistence we have no way to diagnose a
crash or hang reported by a user. This module writes a rotating text
log to the per-user data directory.

Usage:

    from app.gui.app_logger import install_global_logger, log

    install_global_logger()            # call once at startup
    log("model loaded ok")
    log("job 3 failed: ...", level="ERROR")

The log file path is also surfaced in the GUI so users can attach it
to a bug report.
"""

from __future__ import annotations

import datetime
import sys
import threading
import traceback
from pathlib import Path

from app.gui.model_manager import user_data_dir

_LOCK = threading.Lock()
_LOG_PATH: Path | None = None
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB — plenty for any single session


def _redirect_stdio() -> None:
    """Send stdout/stderr to per-user files so they survive --windowed."""
    try:
        target_dir = user_data_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        # line-buffered = updates as soon as Qt writes a warning.
        sys.stdout = open(target_dir / "stdout.txt", "a", encoding="utf-8", buffering=1)
        sys.stderr = open(target_dir / "stderr.txt", "a", encoding="utf-8", buffering=1)
        sys.stdout.write(f"\n--- session {datetime.datetime.now().isoformat(timespec='seconds')} ---\n")
        sys.stderr.write(f"\n--- session {datetime.datetime.now().isoformat(timespec='seconds')} ---\n")
    except OSError:
        # Frozen bundles on some sandbox configurations cannot replace
        # stdio; tolerate that — log.txt is still being written.
        pass


def log_path() -> Path:
    if _LOG_PATH is None:
        return user_data_dir() / "log.txt"
    return _LOG_PATH


def _ensure_path() -> Path:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Coarse rotation: if the file is too big, archive once and start
    # fresh. We don't need a rolling N-file rotation.
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            archive = path.with_suffix(".old.txt")
            try:
                archive.unlink(missing_ok=True)
            except TypeError:
                # Python <3.8 compat (we don't expect to hit this).
                if archive.exists():
                    archive.unlink()
            path.rename(archive)
    except OSError:
        pass
    return path


def log(message: str, level: str = "INFO") -> None:
    """Append a single line to the log file. Never raises."""
    try:
        path = _ensure_path()
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with _LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"{ts} {level:5s} {message}\n")
    except OSError:
        pass


def install_global_logger() -> Path:
    """Pre-create the log file, write a banner, and install excepthook
    so uncaught exceptions also land on disk. Returns the log path."""
    global _LOG_PATH
    _LOG_PATH = user_data_dir() / "log.txt"

    # PyInstaller's --windowed build detaches stdout/stderr to NUL on
    # Windows / /dev/null on macOS. Redirect them to files so Qt's
    # internal warnings (qDebug, qWarning, missing plugin diagnostics,
    # platform-plugin failures) are recoverable.
    _redirect_stdio()

    log("==== Describely starting ====")
    log(f"python={sys.version.split()[0]} platform={sys.platform}")
    log(f"frozen={getattr(sys, 'frozen', False)} exe={sys.executable}")

    def _excepthook(exc_type, exc, tb):
        log(
            "Uncaught exception:\n" + "".join(traceback.format_exception(exc_type, exc, tb)),
            level="ERROR",
        )

    sys.excepthook = _excepthook

    # Also catch Qt-thread crashes — they go through Qt's own handler
    # but PySide6 re-raises into excepthook only on Python ≥3.8. Belt
    # + suspenders: install threading.excepthook too.
    def _thread_excepthook(args):
        log(
            f"Thread {args.thread.name} crashed: "
            + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
            level="ERROR",
        )

    try:
        threading.excepthook = _thread_excepthook
    except AttributeError:
        pass

    return _LOG_PATH
