"""Main window: queue list + per-row progress + cancel buttons.

Deliberately tiny. No options panel — defaults live in worker.py. The
window opens with files already supplied via the CLI args (right-click
flow on Windows / macOS) and starts processing automatically.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.gui.worker import Job, JobStatus, TranscriptionWorker


_STATUS_LABEL = {
    JobStatus.QUEUED: "Queued",
    JobStatus.PROCESSING: "Processing…",
    JobStatus.DONE: "Done",
    JobStatus.FAILED: "Failed",
    JobStatus.CANCELLED: "Cancelled",
}


_URI_SCHEME_PREFIXES = (
    "http:", "https:", "ftp:", "ftps:", "file:",
    "smb:", "afp:", "data:", "javascript:",
)


def _is_safe_local_path(path: Path) -> bool:
    """Reject anything that Explorer / Finder might interpret as a URL
    or a UNC share. UNC paths in particular can leak NTLM hashes if the
    user double-clicks them and Windows authenticates to the remote."""
    text = str(path)
    if text.startswith("\\\\"):
        return False  # UNC path \\server\share
    lowered = text.lower()
    if any(lowered.startswith(scheme) for scheme in _URI_SCHEME_PREFIXES):
        return False
    return path.is_absolute() and path.exists()


def _open_in_file_manager(path: Path) -> None:
    """Reveal ``path`` (or its parent) in Finder/Explorer."""
    if not _is_safe_local_path(path):
        return
    if sys.platform == "win32":
        if path.is_file():
            subprocess.run(["explorer", "/select,", str(path)], check=False)
        else:
            os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.run(["open", "-R", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path.parent)], check=False)


class _JobRow:
    """One row in the queue tree, keyed by ``job_id``."""

    def __init__(self, tree: QTreeWidget, job: Job) -> None:
        self.job_id = job.job_id
        self.item = QTreeWidgetItem([
            job.file_path.name,
            _STATUS_LABEL[job.status],
            "",
            "",
        ])
        self.item.setToolTip(0, str(job.file_path))
        self.item.setData(0, Qt.UserRole, job.job_id)
        tree.addTopLevelItem(self.item)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        tree.setItemWidget(self.item, 2, self.progress)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setFlat(True)
        tree.setItemWidget(self.item, 3, self.cancel_btn)

    def set_status(self, status: JobStatus, message: str = "") -> None:
        label = _STATUS_LABEL.get(status, status.value)
        if message and status == JobStatus.FAILED:
            label = f"{label}: {message[:60]}"
        self.item.setText(1, label)
        if status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            self.cancel_btn.setEnabled(False)
            if status == JobStatus.DONE:
                self.progress.setValue(100)
            self.cancel_btn.setText("—")

    def set_progress(self, fraction: float) -> None:
        self.progress.setValue(int(max(0.0, min(1.0, fraction)) * 100))


class MainWindow(QMainWindow):
    def __init__(
        self,
        initial_files: Optional[list[Path]] = None,
        icon_path: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Describely")
        self.resize(800, 480)
        if icon_path and icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._worker = TranscriptionWorker(self)
        self._rows: dict[int, _JobRow] = {}

        self._build_ui()
        self._connect_signals()
        self._worker.start()

        if initial_files:
            self.add_files(initial_files)

    # ---- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)

        # Toolbar with bulk actions.
        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        act_add_files = QAction("Add Files…", self)
        act_add_folder = QAction("Add Folder…", self)
        act_cancel_all = QAction("Cancel All", self)
        toolbar.addAction(act_add_files)
        toolbar.addAction(act_add_folder)
        toolbar.addSeparator()
        toolbar.addAction(act_cancel_all)
        self._act_add_files = act_add_files
        self._act_add_folder = act_add_folder
        self._act_cancel_all = act_cancel_all

        # Queue tree.
        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["File", "Status", "Progress", ""])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setColumnWidth(0, 320)
        self._tree.setColumnWidth(1, 140)
        self._tree.setColumnWidth(2, 200)
        self._tree.setColumnWidth(3, 100)
        self._tree.itemDoubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self._tree)

        # Bottom bar with a hint.
        bottom = QHBoxLayout()
        self._hint = QLabel(
            "Tip: drag video files here, or right-click in Finder / Explorer "
            "and choose “Create transcription”."
        )
        self._hint.setWordWrap(True)
        bottom.addWidget(self._hint, stretch=1)
        layout.addLayout(bottom)

        self.setCentralWidget(central)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready.")

        self.setAcceptDrops(True)

    def _connect_signals(self) -> None:
        self._act_add_files.triggered.connect(self._on_add_files)
        self._act_add_folder.triggered.connect(self._on_add_folder)
        self._act_cancel_all.triggered.connect(self._on_cancel_all)

        self._worker.job_started.connect(self._on_job_started)
        self._worker.job_progress.connect(self._on_job_progress)
        self._worker.job_finished.connect(self._on_job_finished)
        self._worker.job_log.connect(self._on_job_log)
        self._worker.queue_drained.connect(self._on_queue_drained)

    # ---- Drag-and-drop --------------------------------------------------

    def dragEnterEvent(self, event):  # noqa: N802 (Qt naming)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802 (Qt naming)
        paths: list[Path] = []
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if not local:
                continue
            p = Path(local)
            if p.is_dir():
                paths.extend(self._enumerate_dir(p))
            else:
                paths.append(p)
        if paths:
            self.add_files(paths)

    # ---- Public --------------------------------------------------------

    def add_files(self, paths: list[Path]) -> None:
        cleaned = [Path(p).expanduser().resolve() for p in paths if Path(p).exists()]
        if not cleaned:
            return
        jobs = self._worker.add_files(cleaned)
        for job in jobs:
            row = _JobRow(self._tree, job)
            self._rows[job.job_id] = row
            row.cancel_btn.clicked.connect(lambda _checked=False, jid=job.job_id: self._on_cancel_one(jid))
        self._status.showMessage(f"Added {len(jobs)} file(s).")

    # ---- Slot handlers --------------------------------------------------

    def _on_add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select video or audio files",
            "",
            "Media (*.mp4 *.mov *.mkv *.avi *.webm *.wmv *.flv *.m4v *.mpeg *.mpg *.ts *.3gp "
            "*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus *.wma);;All files (*.*)",
        )
        if paths:
            self.add_files([Path(p) for p in paths])

    def _on_add_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Select a folder with videos")
        if directory:
            self.add_files(self._enumerate_dir(Path(directory)))

    def _on_cancel_all(self) -> None:
        self._worker.cancel_all()
        self._status.showMessage("Cancel request sent for all jobs.")

    def _on_cancel_one(self, job_id: int) -> None:
        self._worker.cancel_job(job_id)

    def _on_job_started(self, job_id: int) -> None:
        row = self._rows.get(job_id)
        if row is not None:
            row.set_status(JobStatus.PROCESSING)
        self._status.showMessage(f"Processing: {self._file_name(job_id)}")

    def _on_job_progress(self, job_id: int, fraction: float) -> None:
        row = self._rows.get(job_id)
        if row is not None:
            row.set_progress(fraction)

    def _on_job_finished(self, job_id: int, status_value: str) -> None:
        row = self._rows.get(job_id)
        if row is None:
            return
        try:
            status = JobStatus(status_value)
        except ValueError:
            status = JobStatus.FAILED
        job = next((j for j in self._worker.snapshot() if j.job_id == job_id), None)
        message = job.error_message if job else ""
        row.set_status(status, message)
        self._status.showMessage(
            f"{self._file_name(job_id)}: {_STATUS_LABEL.get(status, status.value)}"
        )

    def _on_job_log(self, _job_id: int, _message: str) -> None:
        # Logs are stored in the worker's traceback path; UI keeps the status
        # bar clean. Hook left here if a future log panel is added.
        return

    def _on_queue_drained(self) -> None:
        if not self._rows:
            return
        any_active = any(
            job.status in (JobStatus.QUEUED, JobStatus.PROCESSING)
            for job in self._worker.snapshot()
        )
        if not any_active:
            self._status.showMessage("Queue finished.")

    def _on_row_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        job_id = item.data(0, Qt.UserRole)
        job = next((j for j in self._worker.snapshot() if j.job_id == job_id), None)
        if job is None:
            return
        target = job.transcript_path or job.summary_path or job.file_path
        _open_in_file_manager(target)

    # ---- Helpers --------------------------------------------------------

    @staticmethod
    def _enumerate_dir(directory: Path) -> list[Path]:
        suffixes = {
            ".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv", ".m4v",
            ".mpeg", ".mpg", ".ts", ".3gp",
            ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma",
        }
        return sorted(
            p for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in suffixes
        )

    def _file_name(self, job_id: int) -> str:
        row = self._rows.get(job_id)
        return row.item.text(0) if row else f"#{job_id}"

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        active = any(
            j.status == JobStatus.PROCESSING for j in self._worker.snapshot()
        )
        if active:
            reply = QMessageBox.question(
                self,
                "Transcription in progress",
                "Cancel the current job and close the window?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
        self._worker.shutdown()
        event.accept()
