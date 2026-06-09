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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QStatusBar,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.gui.app_logger import log as _file_log
from app.gui.worker import Job, JobMode, JobStatus, TranscribeMode, TranscriptionWorker
from app.version import build_version


_STATUS_LABEL = {
    JobStatus.QUEUED: "Queued",
    JobStatus.PROCESSING: "Processing",
    JobStatus.DONE: "Done",
    JobStatus.FAILED: "Failed",
    JobStatus.CANCELLED: "Cancelled",
}

# How fast the trailing-dots animation in the Status column ticks.
# 400ms gives a clear "still alive" pulse without becoming distracting,
# and is cheap enough (one setText per active row per tick) to be free.
_DOT_PULSE_INTERVAL_MS = 400

_MODE_LABEL = {
    JobMode.BOTH: "Transcription + Summary",
    JobMode.TRANSCRIPTION: "Transcription",
}

_TRANSCRIBE_MODE_LABEL = {
    TranscribeMode.QUALITY: "Quality",
    TranscribeMode.SPEED: "Speed",
}

_TRANSCRIBE_MODE_TOOLTIP = {
    TranscribeMode.QUALITY: (
        "Quality — faster-whisper large-v3, multilingual decoding.\n"
        "English loanwords (\"batch\", \"prompt\", \"wireframe\") stay in "
        "English script. Noticeably slower on macOS and on Windows hosts "
        "without an NVIDIA GPU."
    ),
    TranscribeMode.SPEED: (
        "Speed — whisper.cpp with GPU acceleration where available "
        "(Metal on macOS, Vulkan/CUDA on Windows/Linux). Monolingual: "
        "English loanwords get re-spelled into Ukrainian, but the run "
        "is several times faster."
    ),
}

# Languages exposed in the "Re-summarize as…" submenu. Kept short on
# purpose — the long tail of supported languages is rarely needed for
# the override case (which exists almost exclusively to fix uk↔ru
# misclassification on small text LLMs). ISO-639-1 → display name.
_RESUMMARIZE_LANGUAGES: list[tuple[str, str]] = [
    ("uk", "Ukrainian"),
    ("ru", "Russian"),
    ("en", "English"),
    ("pl", "Polish"),
    ("de", "German"),
    ("fr", "French"),
    ("es", "Spanish"),
]

# Short display labels for the in-row language chip. Bare ISO-639-1
# fits in a small badge and is unambiguous; falling back to the raw
# code keeps the chip useful for languages we didn't name explicitly.
_LANGUAGE_CHIP_LABEL: dict[str, str] = {code: code.upper() for code, _ in _RESUMMARIZE_LANGUAGES}
_LANGUAGE_CHIP_LABEL.update({
    "it": "IT", "pt": "PT", "cs": "CS", "sk": "SK", "be": "BE",
    "tr": "TR", "nl": "NL", "ro": "RO", "ja": "JA", "zh": "ZH",
})

# Probability below which the language chip is considered "uncertain"
# and styled differently (amber) so the user knows the detection was
# shaky and might want to override it.
_LANGUAGE_LOW_CONFIDENCE = 0.6


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


class _ModePickerDialog(QDialog):
    """Asks the user which artifact they want for the new batch, and at
    what quality / speed trade-off.

    Returned via ``selected_mode()`` / ``selected_transcribe_mode()``
    after ``exec()`` == ``Accepted``. The per-row combo in the queue
    can still override the transcribe mode after the fact, so this is
    just the initial value for the whole batch.
    """

    def __init__(
        self,
        file_count: int,
        default_mode: JobMode,
        default_transcribe_mode: TranscribeMode = TranscribeMode.SPEED,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Describely")
        self.setModal(True)
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        header = QLabel(
            f"What should I produce for the {file_count} file(s) you added?"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        self._btn_both = QRadioButton("Both — transcription and summary")
        self._btn_trans = QRadioButton("Transcription only (.transcription.md)")
        layout.addWidget(self._btn_both)
        layout.addWidget(self._btn_trans)

        # Pre-select whatever the toolbar's "current mode" was, so the
        # user can hit Enter to repeat their last choice.
        if default_mode == JobMode.TRANSCRIPTION:
            self._btn_trans.setChecked(True)
        else:
            self._btn_both.setChecked(True)

        # Transcribe mode selector. Speed is the default — it's faster and
        # fine for most recordings; the user opts into Quality (which keeps
        # English loanwords intact via the multilingual decoder) when they
        # need it, and that choice then sticks for the next batches. Speed
        # is listed first so it's the combo's default selection.
        layout.addSpacing(6)
        mode_label = QLabel("Transcription mode:")
        layout.addWidget(mode_label)
        self._tmode_combo = QComboBox()
        for mode in (TranscribeMode.SPEED, TranscribeMode.QUALITY):
            self._tmode_combo.addItem(_TRANSCRIBE_MODE_LABEL[mode], mode.value)
            self._tmode_combo.setItemData(
                self._tmode_combo.count() - 1,
                _TRANSCRIBE_MODE_TOOLTIP[mode],
                Qt.ToolTipRole,
            )
        idx = self._tmode_combo.findData(default_transcribe_mode.value)
        self._tmode_combo.setCurrentIndex(max(0, idx))
        layout.addWidget(self._tmode_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_mode(self) -> JobMode:
        if self._btn_trans.isChecked():
            return JobMode.TRANSCRIPTION
        return JobMode.BOTH

    def selected_transcribe_mode(self) -> TranscribeMode:
        return TranscribeMode(self._tmode_combo.currentData())


class _JobRow:
    """One row in the queue tree, keyed by ``job_id``.

    Columns are: File, Lang, Mode, Type, Status, Progress, Actions.
    The Lang column shows Whisper's detected language as a short chip
    AFTER the job finishes — earlier it's empty. The Mode column hosts
    an editable Quality/Speed combo (only while the job is QUEUED).
    ``Actions`` holds either a Cancel button (while running / queued)
    or Open shortcuts (after completion); the column never grows past
    a couple of icons so the row stays compact."""

    # Column indices kept as constants so a future column reorder
    # touches one place rather than every ``setItemWidget`` / setText
    # call below.
    COL_FILE = 0
    COL_LANG = 1
    COL_MODE = 2
    COL_TYPE = 3
    COL_STATUS = 4
    COL_PROGRESS = 5
    COL_ACTIONS = 6

    def __init__(self, tree: QTreeWidget, job: Job) -> None:
        self.job_id = job.job_id
        self._file_name = job.file_path.name
        self.item = QTreeWidgetItem([
            self._file_name,
            "",  # lang chip — populated on finish
            "",  # mode — rendered via QComboBox widget, see below
            _MODE_LABEL[job.mode],
            _STATUS_LABEL[job.status],
            "",
            "",
        ])
        self.item.setToolTip(self.COL_FILE, str(job.file_path))
        self.item.setData(self.COL_FILE, Qt.UserRole, job.job_id)
        # Centre-align the small text columns so values read as chips
        # rather than left-edge labels.
        self.item.setTextAlignment(self.COL_LANG, Qt.AlignCenter)
        self.item.setTextAlignment(self.COL_STATUS, Qt.AlignLeft | Qt.AlignVCenter)
        tree.addTopLevelItem(self.item)

        # Mode combo: editable until the worker picks the row up.
        # Speed is default; the worker reads the current value at
        # start-of-job, so a change made while the row is queued takes
        # effect on this very job.
        self.mode_combo = QComboBox()
        for mode in (TranscribeMode.SPEED, TranscribeMode.QUALITY):
            self.mode_combo.addItem(_TRANSCRIBE_MODE_LABEL[mode], mode.value)
        current_idx = self.mode_combo.findData(job.transcribe_mode.value)
        self.mode_combo.setCurrentIndex(max(0, current_idx))
        self.mode_combo.setToolTip(_TRANSCRIBE_MODE_TOOLTIP[job.transcribe_mode])
        tree.setItemWidget(self.item, self.COL_MODE, self.mode_combo)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        tree.setItemWidget(self.item, self.COL_PROGRESS, self.progress)

        # Base text for the Status column WITHOUT animated dots. The
        # main window's pulse timer reads this and appends 0..3 dots
        # while the row is in an active state, so we must remember the
        # un-dotted form separately from what's currently rendered.
        self._status_base: str = _STATUS_LABEL[job.status]
        self._is_active: bool = job.status == JobStatus.PROCESSING
        # Latest progress as a whole percent, shown inline in the Status
        # column (e.g. "Processing 47%…") so the user reads the figure
        # without having to look at the narrow progress-bar text. ``None``
        # until the first progress tick or when not actively running.
        self._progress_pct: Optional[int] = None
        # Last dot count rendered by the pulse timer, so a progress tick
        # can re-render the Status cell without losing the current dots.
        self._current_dots: int = 0

        # Actions cell starts as a single Cancel button. After the job
        # reaches a terminal state, ``set_terminal_actions`` rebuilds
        # it with Open Transcript / Open Summary shortcuts and a
        # context-menu hint. We keep a reference to the widget so we
        # can replace it cleanly without leaking children.
        self._actions_widget: Optional[QWidget] = None
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setFlat(True)
        self._set_actions_widget(tree, self.cancel_btn)

    # ---- column helpers -------------------------------------------------

    def _set_actions_widget(self, tree: QTreeWidget, widget: QWidget) -> None:
        self._actions_widget = widget
        tree.setItemWidget(self.item, self.COL_ACTIONS, widget)

    def current_transcribe_mode(self) -> TranscribeMode:
        return TranscribeMode(self.mode_combo.currentData())

    def set_transcribe_mode(self, mode: TranscribeMode) -> None:
        """Sync the combo to ``mode`` without firing the change signal.

        Used to roll the UI back when the worker rejects a mode change
        (e.g. the job already started). Blocking signals prevents an
        infinite ping-pong with the slot that drove the request.
        """
        idx = self.mode_combo.findData(mode.value)
        if idx < 0:
            return
        blocker = self.mode_combo.blockSignals(True)
        try:
            self.mode_combo.setCurrentIndex(idx)
            self.mode_combo.setToolTip(_TRANSCRIBE_MODE_TOOLTIP[mode])
        finally:
            self.mode_combo.blockSignals(blocker)

    def lock_mode(self) -> None:
        """Disable the mode combo once the row is no longer QUEUED.

        Once the worker picks the job up, switching backends mid-run
        would be incoherent — the model is already loaded. We leave the
        combo visible (so the user can see what mode the run used) but
        non-interactive.
        """
        self.mode_combo.setEnabled(False)

    def set_status(self, status: JobStatus, message: str = "") -> None:
        label = _STATUS_LABEL.get(status, status.value)
        if message and status == JobStatus.FAILED:
            label = f"{label}: {message[:60]}"
        elif message and status == JobStatus.DONE:
            # Partial success — transcript landed but summary stage
            # failed inside the pipeline (Ollama down, timeout, OOM).
            # The pipeline doesn't re-raise because the transcription
            # artefact is independent. Surface it here so the user sees
            # WHY there's no .summary.md instead of finding nothing.
            label = f"Done (summary failed)"
            self.item.setToolTip(self.COL_STATUS, message)
        self._status_base = label
        self._is_active = status == JobStatus.PROCESSING
        if status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            # Terminal: drop the inline percent so the label reads clean.
            self._progress_pct = None
            self._current_dots = 0
        self._render_status(self._current_dots)
        if status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            self.cancel_btn.setEnabled(False)
            # Restore determinate bar.
            self.progress.setRange(0, 100)
            if status == JobStatus.DONE:
                self.progress.setValue(100)
        # Any non-queued state freezes the mode selector.
        if status != JobStatus.QUEUED:
            self.lock_mode()

    def set_progress(self, fraction: float) -> None:
        # Restore determinate mode in case we were in busy state.
        if self.progress.maximum() == 0:
            self.progress.setRange(0, 100)
        pct = int(max(0.0, min(1.0, fraction)) * 100)
        self.progress.setValue(pct)
        # Mirror the percent into the Status column so the figure is
        # readable next to the label, not only inside the slim bar.
        self._progress_pct = pct
        self._render_status(self._current_dots)

    def _render_status(self, dots: int) -> None:
        """Compose the Status cell from base label + percent + dots."""
        base = self._status_base.rstrip(".").rstrip("…").rstrip()
        pct = f" {self._progress_pct}%" if (
            self._is_active and self._progress_pct is not None
        ) else ""
        self.item.setText(self.COL_STATUS, f"{base}{pct}" + ("." * dots))

    def set_busy(self, label: str) -> None:
        """Switch to indeterminate (pulsing) progress and update status text."""
        self._status_base = label
        self._is_active = True
        self.item.setText(self.COL_STATUS, label)
        self.progress.setRange(0, 0)  # indeterminate mode

    def apply_dot_animation(self, dots: int) -> None:
        """Append ``dots`` trailing dots to the Status text while active.

        Called by the main window's pulse timer. No-op for rows that
        aren't currently active so terminal states (Done / Failed) stay
        rock-steady. The percent (if any) sits between the label and the
        dots, e.g. "Processing 47%…"."""
        if not self._is_active:
            return
        self._current_dots = dots
        self._render_status(dots)

    def set_language_chip(self, code: Optional[str], probability: Optional[float]) -> None:
        """Render the detected language in the Lang column.

        Empty when ``code`` is missing (transcription-only jobs or
        detection failed). Low-confidence detections get an amber
        tooltip so the user knows the result was shaky and an
        override might be in order."""
        if not code:
            self.item.setText(self.COL_LANG, "")
            self.item.setToolTip(self.COL_LANG, "")
            return
        label = _LANGUAGE_CHIP_LABEL.get(code.lower(), code.upper())
        self.item.setText(self.COL_LANG, label)
        prob_str = f"{probability:.0%}" if probability is not None else "n/a"
        tooltip = f"Detected language: {code} (confidence: {prob_str})"
        if probability is not None and probability < _LANGUAGE_LOW_CONFIDENCE:
            tooltip += "\nLow confidence — right-click to override."
        self.item.setToolTip(self.COL_LANG, tooltip)

    def set_terminal_actions(self, tree: QTreeWidget, job: Job) -> None:
        """Swap the Cancel button for Open Transcript / Open Summary
        shortcuts once the job has finished. Buttons are wired by the
        main window; this method only builds the widget."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.open_transcript_btn = QPushButton("Tr")
        self.open_transcript_btn.setFlat(True)
        self.open_transcript_btn.setToolTip("Open transcription file")
        self.open_transcript_btn.setEnabled(bool(job.transcript_path))
        layout.addWidget(self.open_transcript_btn)

        self.open_summary_btn = QPushButton("Sum")
        self.open_summary_btn.setFlat(True)
        self.open_summary_btn.setToolTip("Open summary file")
        self.open_summary_btn.setEnabled(bool(job.summary_path))
        layout.addWidget(self.open_summary_btn)

        self._set_actions_widget(tree, container)


class MainWindow(QMainWindow):
    def __init__(
        self,
        initial_files: Optional[list[Path]] = None,
        icon_path: Optional[Path] = None,
        initial_mode: JobMode = JobMode.BOTH,
    ) -> None:
        super().__init__()
        _file_log("MainWindow.__init__: super done")
        self.setWindowTitle("Describely")
        self.resize(900, 500)
        if icon_path and icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        _file_log("MainWindow: title + icon set")

        # See worker.py: TranscriptionWorker must not have a parent that
        # lives on the main thread, otherwise Qt cannot route its
        # signals back to us and the main window appears frozen.
        self._worker = TranscriptionWorker()
        self._rows: dict[int, _JobRow] = {}
        # Last picked mode — used as the default selection in the next
        # mode-picker dialog so a user processing a batch of summaries
        # doesn't have to re-click every time.
        self._last_picked_mode: JobMode = initial_mode
        # Same idea for the Quality/Speed selector: remember the last
        # choice so repeat batches don't force a re-click. Speed is the
        # default until the user explicitly picks Quality in the popup;
        # once they do, that choice sticks for the following batches.
        self._last_picked_transcribe_mode: TranscribeMode = TranscribeMode.SPEED
        _file_log("MainWindow: worker constructed")

        self._build_ui()
        _file_log("MainWindow: UI built")
        self._connect_signals()
        _file_log("MainWindow: signals connected")
        self._worker.start()
        _file_log("MainWindow: worker thread started")

        # Single window-wide timer that pulses trailing dots in the
        # Status column of every active row. One QTimer (rather than
        # one-per-row) keeps all rows in sync visually and avoids
        # multiplying timers as the queue grows.
        self._dot_phase = 0
        self._dot_timer = QTimer(self)
        self._dot_timer.setInterval(_DOT_PULSE_INTERVAL_MS)
        self._dot_timer.timeout.connect(self._on_dot_tick)
        self._dot_timer.start()

        # Initial files come from the right-click flow, which has
        # already committed to a mode via the --mode argv flag. Skip
        # the picker dialog for these — they are pre-decided.
        if initial_files:
            self._enqueue(initial_files, mode=initial_mode)
            _file_log(f"MainWindow: queued {len(initial_files)} initial files")

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
        act_pause = QAction("Pause Queue", self)
        act_pause.setCheckable(True)
        act_pause.setToolTip(
            "Pause picks up new jobs only — the current file finishes first."
        )
        act_clear_done = QAction("Clear Completed", self)
        act_clear_done.setToolTip(
            "Remove Done / Failed / Cancelled rows from the list."
        )
        act_cancel_all = QAction("Cancel All", self)
        toolbar.addAction(act_add_files)
        toolbar.addAction(act_add_folder)
        toolbar.addSeparator()
        toolbar.addAction(act_pause)
        toolbar.addAction(act_clear_done)
        toolbar.addSeparator()
        toolbar.addAction(act_cancel_all)
        self._act_add_files = act_add_files
        self._act_add_folder = act_add_folder
        self._act_pause = act_pause
        self._act_clear_done = act_clear_done
        self._act_cancel_all = act_cancel_all

        # Queue tree. Columns: File, Lang, Mode, Type, Status, Progress, Actions.
        # Mode hosts a per-row Quality/Speed combo (editable while queued)
        # so the user can mix modes within one batch without having to
        # re-add files.
        self._tree = QTreeWidget()
        self._tree.setColumnCount(7)
        self._tree.setHeaderLabels(
            ["File", "Lang", "Mode", "Type", "Status", "Progress", ""]
        )
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setColumnWidth(0, 260)
        self._tree.setColumnWidth(1, 56)
        self._tree.setColumnWidth(2, 110)
        self._tree.setColumnWidth(3, 160)
        self._tree.setColumnWidth(4, 130)
        self._tree.setColumnWidth(5, 170)
        self._tree.setColumnWidth(6, 110)
        # Let the File column absorb extra space; Lang stays narrow.
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        # Internal-move drag-and-drop reorders QUEUED rows in place.
        # Qt fires ``model().rowsMoved`` when the user drops a row;
        # we hook that to push the new order to the worker.
        self._tree.setDragDropMode(QAbstractItemView.InternalMove)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemDoubleClicked.connect(self._on_row_double_clicked)
        # Qt fires ``rowsMoved`` on its internal model when a drag
        # completes; we read the resulting order and tell the worker.
        self._tree.model().rowsMoved.connect(self._on_rows_moved)
        layout.addWidget(self._tree)

        # Bottom hint — only visible when the queue is empty so it
        # doesn't crowd the window once the user is busy.
        bottom = QHBoxLayout()
        self._hint = QLabel(
            "Tip: drag video files here, or right-click in Finder / Explorer "
            "and choose “Create transcription” / “Create summary”."
        )
        self._hint.setWordWrap(True)
        bottom.addWidget(self._hint, stretch=1)
        layout.addLayout(bottom)

        self.setCentralWidget(central)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready.")
        # Show the running build version in the bottom-right corner. A
        # permanent widget isn't overwritten by transient showMessage calls.
        _version_label = QLabel(f"Build {build_version()}")
        _version_label.setStyleSheet("color: gray; margin-right: 6px;")
        self._status.addPermanentWidget(_version_label)

        self.setAcceptDrops(True)
        self._refresh_hint_visibility()

    def _connect_signals(self) -> None:
        self._act_add_files.triggered.connect(self._on_add_files)
        self._act_add_folder.triggered.connect(self._on_add_folder)
        self._act_pause.toggled.connect(self._on_pause_toggled)
        self._act_clear_done.triggered.connect(self._on_clear_completed)
        self._act_cancel_all.triggered.connect(self._on_cancel_all)

        self._worker.job_started.connect(self._on_job_started)
        self._worker.job_progress.connect(self._on_job_progress)
        self._worker.job_finished.connect(self._on_job_finished)
        self._worker.job_log.connect(self._on_job_log)
        self._worker.queue_drained.connect(self._on_queue_drained)
        self._worker.pause_state_changed.connect(self._on_pause_state_changed)

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
            self._add_with_picker(paths)

    # ---- Public --------------------------------------------------------

    def add_files(
        self, paths: list[Path], *, mode: Optional[JobMode] = None
    ) -> None:
        """Add files to the queue from an external trigger.

        Used by the macOS file-open event filter and the single-instance
        IPC handler (a second Quick Action invocation forwarded here).

        ``mode`` set → the trigger already decided the artifacts (the
        right-click verb's ``--mode``); enqueue without prompting. ``mode``
        is None → no verb decided for us (e.g. a bare macOS file-open
        event), so fall back to the picker dialog.
        """
        cleaned = [
            Path(p).expanduser().resolve()
            for p in paths
            if Path(p).expanduser().exists()
        ]
        if not cleaned:
            return
        if mode is None:
            self._add_with_picker(cleaned)
        else:
            self._enqueue(cleaned, mode=mode)

    def _add_with_picker(self, paths: list[Path]) -> None:
        """User-initiated add — prompts for mode, then enqueues."""
        cleaned = [Path(p).expanduser().resolve() for p in paths if Path(p).exists()]
        if not cleaned:
            return
        dialog = _ModePickerDialog(
            file_count=len(cleaned),
            default_mode=self._last_picked_mode,
            default_transcribe_mode=self._last_picked_transcribe_mode,
            parent=self,
        )
        dialog.setWindowIcon(self.windowIcon())
        if dialog.exec() != QDialog.Accepted:
            self._status.showMessage("Add cancelled.")
            return
        mode = dialog.selected_mode()
        transcribe_mode = dialog.selected_transcribe_mode()
        self._last_picked_mode = mode
        self._last_picked_transcribe_mode = transcribe_mode
        self._enqueue(cleaned, mode=mode)

    def _enqueue(self, paths: list[Path], *, mode: JobMode) -> None:
        """Add already-validated paths to the worker queue."""
        if not paths:
            return
        jobs = self._worker.add_files(
            paths, mode=mode, transcribe_mode=self._last_picked_transcribe_mode
        )
        for job in jobs:
            row = _JobRow(self._tree, job)
            self._rows[job.job_id] = row
            row.cancel_btn.clicked.connect(
                lambda _checked=False, jid=job.job_id: self._on_cancel_one(jid)
            )
            # Drive the worker from the per-row combo so the user can
            # change Quality↔Speed any time before the job starts.
            row.mode_combo.currentIndexChanged.connect(
                lambda _idx, jid=job.job_id: self._on_row_mode_changed(jid)
            )
        self._status.showMessage(
            f"Added {len(jobs)} file(s) — {_MODE_LABEL[mode]}."
        )
        self._refresh_hint_visibility()

    def _on_row_mode_changed(self, job_id: int) -> None:
        """Forward a per-row mode change to the worker.

        If the worker rejects it (job already running), roll the combo
        back so the displayed value never lies about what the job is
        actually using.
        """
        row = self._rows.get(job_id)
        if row is None:
            return
        new_mode = row.current_transcribe_mode()
        if self._worker.set_transcribe_mode(job_id, new_mode):
            # Tooltip needs to follow the selected mode so the
            # explainer text matches the chip.
            row.mode_combo.setToolTip(_TRANSCRIBE_MODE_TOOLTIP[new_mode])
            return
        # Rejected — figure out the actual mode from the worker and
        # snap the combo back so the UI stays honest.
        current = self._worker.find_job(job_id)
        if current is not None:
            row.set_transcribe_mode(current.transcribe_mode)
        self._status.showMessage(
            "Mode change ignored: job already started."
        )

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
            self._add_with_picker([Path(p) for p in paths])

    def _on_add_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Select a folder with videos")
        if directory:
            self._add_with_picker(self._enumerate_dir(Path(directory)))

    def _on_cancel_all(self) -> None:
        self._worker.cancel_all()
        # Wipe every still-pending row immediately. Rows that already
        # finished (Done / Failed) stay so the user can review them.
        for job_id in list(self._rows):
            self._remove_row_if_pending(job_id)
        self._status.showMessage("Cancelled all pending jobs.")

    def _on_cancel_one(self, job_id: int) -> None:
        self._worker.cancel_job(job_id)
        # Single-job cancel always removes the row, per UX request.
        # If the job was already running, the worker still emits a
        # ``job_finished`` signal but the row is gone, so _on_job_finished
        # gracefully no-ops.
        self._remove_row(job_id)
        self._status.showMessage("Job removed.")

    def _remove_row(self, job_id: int) -> None:
        row = self._rows.pop(job_id, None)
        if row is None:
            return
        idx = self._tree.indexOfTopLevelItem(row.item)
        if idx >= 0:
            self._tree.takeTopLevelItem(idx)
        self._refresh_hint_visibility()

    def _remove_row_if_pending(self, job_id: int) -> None:
        row = self._rows.get(job_id)
        if row is None:
            return
        current_status = row.item.text(_JobRow.COL_STATUS)
        # Rows in a terminal state ("Done" / "Failed: …") are preserved
        # so the user can still see / open the produced files.
        if current_status in (_STATUS_LABEL[JobStatus.DONE],):
            return
        if current_status.startswith(_STATUS_LABEL[JobStatus.FAILED]):
            return
        self._remove_row(job_id)

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
            # Row was already removed via cancel — nothing to update.
            return
        try:
            status = JobStatus(status_value)
        except ValueError:
            status = JobStatus.FAILED
        job = next((j for j in self._worker.snapshot() if j.job_id == job_id), None)
        message = job.error_message if job else ""
        row.set_status(status, message)
        if job is not None:
            row.set_language_chip(
                job.detected_language, job.detected_language_probability
            )
            if status == JobStatus.DONE:
                row.set_terminal_actions(self._tree, job)
                row.open_transcript_btn.clicked.connect(
                    lambda _checked=False, jid=job_id: self._open_output(jid, "transcript")
                )
                row.open_summary_btn.clicked.connect(
                    lambda _checked=False, jid=job_id: self._open_output(jid, "summary")
                )
        self._status.showMessage(
            f"{self._file_name(job_id)}: {_STATUS_LABEL.get(status, status.value)}"
        )

    def _on_job_log(self, job_id: int, message: str) -> None:
        row = self._rows.get(job_id)
        if row is None:
            return
        if message.startswith("summarize:"):
            row.set_busy("Summarizing")
            self._status.showMessage(f"Summarizing: {self._file_name(job_id)}")
        elif message.startswith("cleanup:"):
            row.set_busy("Cleaning up")
        elif message.startswith("transcribe:"):
            row.set_status(JobStatus.PROCESSING)
            self._status.showMessage(f"Transcribing: {self._file_name(job_id)}")
        elif message.startswith("WARNING:"):
            self._status.showMessage(f"⚠ {message[len('WARNING:'):].strip()}")

    def _on_dot_tick(self) -> None:
        """Advance the trailing-dots animation for active rows.

        Cycles through 0..3 dots. If there are no active rows the loop
        is a handful of dict lookups — cheap enough to leave the timer
        running unconditionally rather than starting/stopping it on
        every queue state change."""
        self._dot_phase = (self._dot_phase + 1) % 4
        for row in self._rows.values():
            row.apply_dot_animation(self._dot_phase)

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
        self._open_output(job_id, "auto")

    def _on_pause_toggled(self, checked: bool) -> None:
        self._worker.set_paused(checked)

    def _on_pause_state_changed(self, paused: bool) -> None:
        # ``pause_state_changed`` round-trips from the worker so the
        # checkbox label/state stays correct even if pause was set
        # programmatically. Block signals to avoid a feedback loop
        # when the GUI thread is the one that flipped it.
        self._act_pause.blockSignals(True)
        try:
            self._act_pause.setChecked(paused)
            self._act_pause.setText("Resume Queue" if paused else "Pause Queue")
        finally:
            self._act_pause.blockSignals(False)
        if paused:
            self._status.showMessage(
                "Queue paused — current file will finish, no new jobs will start."
            )
        else:
            self._status.showMessage("Queue resumed.")

    def _on_clear_completed(self) -> None:
        removed = self._worker.remove_finished()
        for job_id in removed:
            self._remove_row(job_id)
        if removed:
            self._status.showMessage(f"Cleared {len(removed)} completed row(s).")
        else:
            self._status.showMessage("Nothing to clear — no completed jobs.")
        self._refresh_hint_visibility()

    def _on_rows_moved(self, *_args) -> None:
        # ``rowsMoved`` fires for every internal-move drag. Snapshot
        # the new visual order from the tree, then ask the worker to
        # apply it. Worker enforces "only QUEUED jobs actually move",
        # so dragging a Done row above a Queued one is a visual no-op.
        ordered_ids: list[int] = []
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            job_id = item.data(0, Qt.UserRole)
            if job_id is not None:
                ordered_ids.append(int(job_id))
        self._worker.reorder_queued(ordered_ids)

    def _on_context_menu(self, position) -> None:
        item = self._tree.itemAt(position)
        if item is None:
            return
        job_id = item.data(0, Qt.UserRole)
        job = next((j for j in self._worker.snapshot() if j.job_id == job_id), None)
        if job is None:
            return

        menu = QMenu(self._tree)
        # File-opening shortcuts: only enabled when the path exists,
        # so the user can't trigger a "file not found" surprise.
        act_open_transcript = menu.addAction("Open transcription")
        act_open_transcript.setEnabled(bool(job.transcript_path))
        act_open_summary = menu.addAction("Open summary")
        act_open_summary.setEnabled(bool(job.summary_path))
        act_open_source = menu.addAction("Reveal source file")
        act_open_source.setEnabled(job.file_path.exists())
        menu.addSeparator()

        # Re-summarize submenu — only when there's a finished job to
        # re-run AND we have a transcript or source we could re-feed.
        if job.status == JobStatus.DONE:
            resub = menu.addMenu("Re-run as language…")
            for code, name in _RESUMMARIZE_LANGUAGES:
                action = resub.addAction(name)
                action.setData(code)
                action.triggered.connect(
                    lambda _checked=False, c=code: self._on_resummarize(job_id, c)
                )

        menu.addSeparator()
        act_remove = menu.addAction("Remove from list")
        # Removing a still-running row also cancels it — same semantics
        # as the per-row Cancel button used to provide.
        act_remove.triggered.connect(lambda: self._on_cancel_one(job_id))

        chosen = menu.exec(self._tree.viewport().mapToGlobal(position))
        if chosen is act_open_transcript:
            self._open_output(job_id, "transcript")
        elif chosen is act_open_summary:
            self._open_output(job_id, "summary")
        elif chosen is act_open_source:
            _open_in_file_manager(job.file_path)

    def _on_resummarize(self, job_id: int, language_code: str) -> None:
        job = next((j for j in self._worker.snapshot() if j.job_id == job_id), None)
        if job is None:
            return
        # Re-running enqueues a NEW job for the same source file with
        # the language forced. The original row stays so the user can
        # still see / open the previous output if they wanted to keep
        # both for comparison.
        jobs = self._worker.add_files(
            [job.file_path], mode=job.mode, forced_language=language_code
        )
        for new_job in jobs:
            row = _JobRow(self._tree, new_job)
            self._rows[new_job.job_id] = row
            row.cancel_btn.clicked.connect(
                lambda _checked=False, jid=new_job.job_id: self._on_cancel_one(jid)
            )
        # Show the forced language as a chip on the NEW row right away
        # so the user can confirm the override was applied without
        # waiting for the job to finish.
        if jobs:
            row = self._rows.get(jobs[0].job_id)
            if row is not None:
                row.set_language_chip(language_code, None)
        self._status.showMessage(
            f"Re-running {job.file_path.name} forced to {language_code}."
        )
        self._refresh_hint_visibility()

    def _open_output(self, job_id: int, kind: str) -> None:
        """``kind`` is ``"transcript"``, ``"summary"``, or ``"auto"``
        (transcript first, summary as fallback, source as last resort)."""
        job = next((j for j in self._worker.snapshot() if j.job_id == job_id), None)
        if job is None:
            return
        if kind == "transcript":
            target = job.transcript_path
        elif kind == "summary":
            target = job.summary_path
        else:
            target = job.transcript_path or job.summary_path or job.file_path
        if target is None:
            self._status.showMessage("No file to open for that action.")
            return
        _open_in_file_manager(target)

    def _refresh_hint_visibility(self) -> None:
        """Show the drag-and-drop hint only when the queue is empty —
        once there are rows on screen, the visible UI itself is the
        affordance and the hint is dead weight."""
        self._hint.setVisible(not self._rows)

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
        return row.item.text(_JobRow.COL_FILE) if row else f"#{job_id}"

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
        self._dot_timer.stop()
        self._worker.shutdown()
        event.accept()
