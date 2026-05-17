"""First-launch Terms of Use acceptance gate.

Logging note: every non-trivial branch here writes a line to the file
log so we can localize hangs/crashes without a debugger.


Shows a modal dialog with the bundled TERMS.md on first launch. The user
must explicitly accept before any media file is processed. Acceptance is
recorded in a small flag file under the per-user data directory, keyed
on a version number — bumping the version (e.g. ``terms-accepted-v2``)
forces re-acceptance after a significant TOS change.

Why a runtime gate in addition to the installer's license screen:
* The Windows Inno Setup installer already requires acceptance, but the
  macOS DMG flow does not — users can drag the .app to Applications and
  bypass the bundled TERMS.md entirely. A modal closes that gap.
* In dev mode (`python -m app.gui`), no installer ran at all.
* Per-version flag lets us notify existing users about future TOS edits.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from app.gui.app_logger import log as _file_log
from app.gui.model_manager import user_data_dir

TERMS_VERSION = "v1"
ACCEPTANCE_FILENAME = f"terms-accepted-{TERMS_VERSION}.flag"


def _flag_path() -> Path:
    return user_data_dir() / ACCEPTANCE_FILENAME


def has_accepted_terms() -> bool:
    return _flag_path().exists()


def record_acceptance() -> None:
    flag = _flag_path()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(
        "Describely TERMS accepted.\n"
        f"version={TERMS_VERSION}\n",
        encoding="utf-8",
    )


def _resolve_terms_file() -> Optional[Path]:
    """Locate TERMS.md in the same locations we'd look for icon.svg."""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "TERMS.md")
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates += [
            exe_dir / "TERMS.md",
            exe_dir.parent / "Resources" / "TERMS.md",
        ]
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / "TERMS.md")
    for c in candidates:
        if c.exists():
            return c
    return None


class TermsDialog(QDialog):
    """Modal Terms of Use prompt. ``exec()`` returns ``QDialog.Accepted``
    when the user ticks the box and clicks Accept, else ``Rejected``."""

    def __init__(
        self,
        terms_text: str,
        icon: Optional[QIcon] = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Describely — Terms of Use")
        self.setModal(True)
        self.resize(720, 560)
        if icon is not None:
            self.setWindowIcon(icon)

        layout = QVBoxLayout(self)

        header = QLabel(
            "Please review and accept the Terms of Use before continuing. "
            "You can read the full text below; the same file ships with "
            "the application."
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        self._viewer = QTextBrowser()
        self._viewer.setOpenExternalLinks(False)
        # ``QTextBrowser.setMarkdown`` has historically hung Qt 6 main
        # thread on certain inputs (notably markdown tables, which we
        # have in TERMS.md). Try it first for the pretty rendering;
        # silently fall back to plain text on ANY failure so a
        # malformed-document never bricks the app.
        _file_log("TermsDialog: setMarkdown begin")
        try:
            self._viewer.setMarkdown(terms_text)
            _file_log("TermsDialog: setMarkdown ok")
        except Exception as exc:
            _file_log(f"TermsDialog: setMarkdown failed ({exc}); using plain text", level="WARN")
            self._viewer.setPlainText(terms_text)
        layout.addWidget(self._viewer, stretch=1)

        self._checkbox = QCheckBox(
            "I have read and agree to the Describely Terms of Use."
        )
        self._checkbox.stateChanged.connect(self._update_accept_state)
        layout.addWidget(self._checkbox)

        button_box = QDialogButtonBox(self)
        self._accept_btn = button_box.addButton(
            "Accept and Continue", QDialogButtonBox.AcceptRole
        )
        self._reject_btn = button_box.addButton(
            "Quit", QDialogButtonBox.RejectRole
        )
        self._accept_btn.setEnabled(False)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _update_accept_state(self) -> None:
        self._accept_btn.setEnabled(self._checkbox.isChecked())


def ensure_terms_accepted(icon: Optional[QIcon] = None) -> bool:
    """Show the modal if needed; return True only if the user accepts.

    Safe to call before any window is shown — instantiates its own
    QDialog parented to the current QApplication.
    """
    if has_accepted_terms():
        _file_log("TERMS already accepted; skipping modal")
        return True
    _file_log("TERMS flag missing; resolving TERMS file…")

    terms_path = _resolve_terms_file()
    if terms_path is None:
        _file_log("TERMS file missing from install", level="ERROR")
        # Fallback: if we somehow shipped without TERMS.md, fail closed
        # rather than silently continuing.
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(
            None,
            "Describely",
            "Terms of Use file is missing from this installation. "
            "Reinstall Describely and try again.",
        )
        return False

    _file_log(f"reading TERMS from {terms_path}")
    text = terms_path.read_text(encoding="utf-8")
    _file_log(f"TERMS loaded: {len(text)} chars; constructing dialog")
    # Ensure QApplication exists — needed when this is called from main()
    # before the main window is constructed.
    if QApplication.instance() is None:
        QApplication([])

    dialog = TermsDialog(text, icon=icon)
    _file_log("TermsDialog: exec()")
    result = dialog.exec()
    _file_log(f"TermsDialog: returned {result}")
    if result == QDialog.Accepted:
        record_acceptance()
        return True
    return False
