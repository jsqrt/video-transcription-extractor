"""Entry point: ``python -m app.gui [FILE ...]``.

Network isolation is applied first (same posture as the CLI). Then we
spin up the Qt application and feed any positional file arguments to
the main window so the right-click flow can launch us with files
pre-loaded.

On macOS, Finder / Launch Services delivers selected files via
``QFileOpenEvent`` rather than argv. We install an application-level
event filter that forwards those into ``MainWindow.add_files``.
"""

from __future__ import annotations

# Match CLI behaviour: lock outbound network to loopback before importing
# anything else that might phone home.
from app.security.network_isolation import enforce_offline_mode

enforce_offline_mode()

import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QFileOpenEvent, QIcon
from PySide6.QtWidgets import QApplication

from app.gui.app_logger import install_global_logger, log
from app.gui.first_run import ensure_terms_accepted
from app.gui.macos_integration import install_quick_action
from app.gui.main_window import MainWindow
from app.gui.single_instance import PrimaryServer, forward_to_primary
from app.gui.update_prompt import maybe_prompt_update
from app.gui.worker import JobMode

APP_NAME = "Describely"
ORG_NAME = "Describely"
ORG_DOMAIN = "describely.app"
APP_VERSION = "1.0.0"


_MODE_FLAG = "--mode"
# Maps the ``--mode`` values the OS context-menu verbs pass into the two
# JobMode members. There is no summary-only JobMode (a summary always
# needs a transcript), so the "Create summary" right-click verb — which
# the Windows installer and the macOS Quick Action both register with
# ``--mode summary`` — resolves to BOTH (transcript + summary). Without
# this alias "summary" was unrecognised and silently fell through to the
# default, which happened to be BOTH too, but only by accident; making it
# explicit keeps the menu label honest and survives a default change.
_MODE_ALIASES = {
    "both": JobMode.BOTH,
    "summary": JobMode.BOTH,
    "transcription": JobMode.TRANSCRIPTION,
}


def _parse_mode(argv: list[str]) -> JobMode:
    """Pull ``--mode VALUE`` (or ``--mode=VALUE``) out of argv.

    Default is BOTH so right-click produces both artifacts unless the verb
    explicitly asks for transcription only. The shell verbs registered by
    Inno Setup / the macOS workflow set this explicitly per menu item.
    """
    for i, token in enumerate(argv):
        value: str | None = None
        if token == _MODE_FLAG and i + 1 < len(argv):
            value = argv[i + 1]
        elif token.startswith(f"{_MODE_FLAG}="):
            value = token.split("=", 1)[1]
        if value is not None and value.lower() in _MODE_ALIASES:
            return _MODE_ALIASES[value.lower()]
    return JobMode.BOTH


def _expand_args(argv: list[str]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    skip_next = False
    for i, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if not token:
            continue
        if token == _MODE_FLAG:
            skip_next = True  # the next token is the mode value
            continue
        if token.startswith("-"):
            continue
        try:
            p = Path(token).expanduser().resolve()
        except (OSError, ValueError):
            continue
        if not p.exists() or not p.is_file():
            continue
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _resolve_icon() -> Path | None:
    """Find the SVG icon next to the binary (frozen) or in the repo (dev)."""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "icon.svg")
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates += [
            exe_dir / "icon.svg",
            exe_dir.parent / "Resources" / "icon.svg",
        ]
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / "build" / "assets" / "icon.svg")
    for c in candidates:
        if c.exists():
            return c
    return None


class _FileOpenFilter(QObject):
    """Forwards ``QFileOpenEvent`` to the active window.

    Buffers events until ``window`` is set so files dropped onto a not-yet-
    constructed window during macOS launch aren't lost.
    """

    def __init__(self) -> None:
        super().__init__()
        self.window: MainWindow | None = None
        self._pending: list[Path] = []

    def attach(self, window: MainWindow) -> None:
        self.window = window
        if self._pending:
            window.add_files(self._pending)
            self._pending.clear()

    def eventFilter(self, _obj, event):  # noqa: N802 (Qt naming)
        if isinstance(event, QFileOpenEvent) or event.type() == QEvent.FileOpen:
            path = Path(event.file()).expanduser()
            if path.exists() and path.is_file():
                if self.window is not None:
                    self.window.add_files([path])
                else:
                    self._pending.append(path)
                return True
        return False


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # File-logger first — captures everything from this point onward,
    # including stdlib warnings about model loading and any uncaught
    # exception from the QThread worker. Survives ``--windowed`` builds
    # that discard stdout/stderr.
    log_file = install_global_logger()
    log(f"argv={argv}")

    files = _expand_args(argv)
    mode = _parse_mode(argv)
    log(f"resolved {len(files)} input file(s); mode={mode.value}")

    log("constructing QApplication...")
    app = QApplication(sys.argv)
    log("QApplication ready")
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setOrganizationDomain(ORG_DOMAIN)
    app.setApplicationVersion(APP_VERSION)
    log("app metadata set")

    # Single-instance handoff: if a primary window is already open, hand it
    # our files + mode and exit. This is the path that fixes "right-click →
    # Create Summary does nothing when Describely is already running": the
    # Quick Action launches us afresh (open -n -a), and we forward instead
    # of opening a second, redundant window. When no primary exists we fall
    # through and become the primary ourselves (server started below).
    if forward_to_primary(mode.value, files):
        log("forwarded to existing instance; exiting courier process")
        return 0
    log("no existing instance; continuing as primary")

    icon_path = _resolve_icon()
    log(f"icon_path={icon_path}")
    app_icon = QIcon(str(icon_path)) if icon_path is not None else None
    if app_icon is not None:
        app.setWindowIcon(app_icon)
    log("window icon set")

    # Gate the rest of the app on first-run Terms acceptance. The dialog
    # records the decision under user_data_dir; subsequent launches skip
    # it instantly. If the user declines, we exit with code 0 (no error,
    # but no work performed).
    log("checking TERMS acceptance...")
    if not ensure_terms_accepted(icon=app_icon):
        log("TERMS not accepted, exiting")
        return 0
    log("TERMS accepted (or pre-recorded)")

    # macOS first-launch: auto-register the Finder Quick Action so the
    # user gets right-click → Quick Actions → Create Transcription
    # without running a separate installer. No-op on Win / Linux and
    # idempotent (subsequent launches skip via a flag file). Failure is
    # silent — the .app still works, only the right-click entry is
    # missing.
    log("install_quick_action()...")
    install_quick_action()
    log("install_quick_action returned")

    open_filter = _FileOpenFilter()
    app.installEventFilter(open_filter)
    log("file open filter installed")

    log("constructing MainWindow...")
    window = MainWindow(
        initial_files=files,
        icon_path=icon_path,
        initial_mode=mode,
    )
    open_filter.attach(window)
    log("MainWindow constructed; showing")
    window.show()

    # As the primary, start listening for couriers (subsequent Quick Action
    # invocations). Each delivers a mode + file list which we enqueue with
    # the verb's mode, exactly as a fresh launch would, and bring the window
    # forward so the user sees the queue update. Kept on a local that lives
    # for the duration of app.exec().
    def _on_forwarded(mode_value: str, paths: list[Path]) -> None:
        forwarded_mode = _MODE_ALIASES.get(mode_value.lower(), JobMode.BOTH)
        log(f"primary received {len(paths)} forwarded file(s); mode={forwarded_mode.value}")
        window.add_files(paths, mode=forwarded_mode)
        if window.isMinimized():
            window.showNormal()
        window.raise_()
        window.activateWindow()

    primary_server = PrimaryServer(_on_forwarded)
    if not primary_server.start():
        log("primary server failed to start; running without single-instance IPC")

    # Monthly update nag. No-ops on first ever launch (just seeds the
    # timestamp) and on subsequent launches until 30 days have passed.
    log("maybe_prompt_update()...")
    maybe_prompt_update(parent=window, icon=app_icon)
    log("maybe_prompt_update returned")

    log("entering Qt event loop")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
