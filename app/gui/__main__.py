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

from app.gui.first_run import ensure_terms_accepted
from app.gui.main_window import MainWindow

APP_NAME = "Describely"
ORG_NAME = "Describely"
ORG_DOMAIN = "describely.app"
APP_VERSION = "1.0.0"


def _expand_args(argv: list[str]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for token in argv:
        if not token or token.startswith("-"):
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
    files = _expand_args(argv)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setOrganizationDomain(ORG_DOMAIN)
    app.setApplicationVersion(APP_VERSION)

    icon_path = _resolve_icon()
    app_icon = QIcon(str(icon_path)) if icon_path is not None else None
    if app_icon is not None:
        app.setWindowIcon(app_icon)

    # Gate the rest of the app on first-run Terms acceptance. The dialog
    # records the decision under user_data_dir; subsequent launches skip
    # it instantly. If the user declines, we exit with code 0 (no error,
    # but no work performed).
    if not ensure_terms_accepted(icon=app_icon):
        return 0

    open_filter = _FileOpenFilter()
    app.installEventFilter(open_filter)

    window = MainWindow(initial_files=files, icon_path=icon_path)
    open_filter.attach(window)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
