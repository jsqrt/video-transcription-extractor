"""Background worker that processes one file at a time.

Lives on its own QThread so the Qt event loop stays responsive while
Whisper crunches audio. Owns one cancel event per active job so the UI
can either cancel the *current* file (skip to next) or the *entire*
queue (drain everything, set every pending job's status to ``cancelled``).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from app.gui.app_logger import log as _file_log
from app.gui.model_manager import embedded_model_name, find_embedded_model_path
from app.models.types import SummaryOptions, TranscribeOptions
from app.providers.faster_whisper_provider import FasterWhisperProvider
from app.services.audio_extractor import AudioExtractor
from app.services.pipeline import run_pipeline
from app.services.summarizer import Summarizer
from app.services.summary_writer import SummaryWriter
from app.services.transcriber import Transcriber
from app.services.writer import CleanTranscriptWriter


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobMode(str, Enum):
    """What artifacts to produce for a single job."""

    BOTH = "both"             # .transcription.md + .summary.md
    TRANSCRIPTION = "transcription"  # .transcription.md only
    SUMMARY = "summary"       # .summary.md only


@dataclass
class Job:
    job_id: int
    file_path: Path
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    error_message: str = ""
    output_dir: Optional[Path] = None
    transcript_path: Optional[Path] = None
    summary_path: Optional[Path] = None
    mode: JobMode = JobMode.BOTH
    cancel_event: threading.Event = field(default_factory=threading.Event)


class _PipelineServices:
    """Built once and reused across the queue so the Whisper model is
    loaded only on the first job and cached afterwards.

    Constructed lazily on the worker thread because importing
    faster-whisper can be slow.
    """

    def __init__(self) -> None:
        self._provider: Optional[FasterWhisperProvider] = None
        self._transcriber: Optional[Transcriber] = None
        self._summary_writer: Optional[SummaryWriter] = None
        self._clean_writer: Optional[CleanTranscriptWriter] = None
        self._extractor: Optional[AudioExtractor] = None

    def transcriber(self, logger_fn) -> Transcriber:
        if self._transcriber is None:
            # When the model is bundled we point the provider at the model
            # directory directly; faster-whisper accepts an on-disk path
            # in place of a short name, which skips HF cache resolution
            # entirely.
            self._provider = FasterWhisperProvider()
            self._transcriber = Transcriber(
                provider=self._provider, logger_fn=logger_fn
            )
        else:
            # Keep the latest job's logger so per-file warnings reach the UI.
            self._transcriber._logger = logger_fn  # noqa: SLF001
        return self._transcriber

    def extractor(self) -> AudioExtractor:
        if self._extractor is None:
            self._extractor = AudioExtractor(sample_rate=16000)
        return self._extractor

    def summarizer(self, logger_fn) -> Summarizer:
        """Pick a backend for this run, in priority order:

        1. **Ollama** at ``127.0.0.1:11434`` if reachable. The user
           explicitly running an Ollama daemon means they chose their
           model (usually larger / better than what we bundle), so we
           defer to it.
        2. **Bundled llama.cpp** with the GGUF shipped under
           ``models/llm/``. Runs locally inside our process, no setup
           required, but limited to the small Qwen we shipped.
        3. **Extractive** — pure-Python sentence picker. Last resort
           when neither LLM is available (e.g. dev checkout without
           ``scripts/fetch_llm.py`` and no Ollama).

        Re-probed every job so the user can start / stop Ollama
        between files and the next one picks the new backend.
        """
        options = SummaryOptions(mode="ollama")

        # 1) Ollama — probe server AND verify the model is actually installed
        from app.providers.ollama_provider import OllamaClient

        probe = OllamaClient(
            base_url=options.ollama_base_url,
            model=options.ollama_model,
            timeout_sec=options.ollama_timeout_sec,
        )
        installed_models = probe.list_models()  # [] when server is down

        if not installed_models:
            # Try to auto-start Ollama if it's installed but not running.
            ollama_exe = shutil.which("ollama")
            if ollama_exe is None:
                # Common Windows install location
                candidate = Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"
                if candidate.is_file():
                    ollama_exe = str(candidate)
            if ollama_exe:
                logger_fn("Ollama not running — starting it automatically…")
                try:
                    subprocess.Popen(
                        [ollama_exe, "serve"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    # Wait up to 10 s for the server to become ready.
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        time.sleep(0.5)
                        installed_models = probe.list_models()
                        if installed_models:
                            logger_fn("Ollama started successfully.")
                            break
                    else:
                        logger_fn("Ollama did not respond in time; falling back.")
                except OSError:
                    pass

        if installed_models:
            # Pick the configured model if installed, else the first available one.
            chosen_model = (
                options.ollama_model
                if options.ollama_model in installed_models
                else installed_models[0]
            )
            ollama = OllamaClient(
                base_url=options.ollama_base_url,
                model=chosen_model,
                timeout_sec=options.ollama_timeout_sec,
            )
            chosen_options = SummaryOptions(
                mode="ollama",
                ollama_base_url=options.ollama_base_url,
                ollama_model=chosen_model,
                ollama_timeout_sec=options.ollama_timeout_sec,
            )
            logger_fn(f"Summary backend: Ollama ({chosen_model}).")
            return Summarizer(
                options=chosen_options, llm_client=ollama, logger_fn=logger_fn
            )

        # 2) Bundled llama.cpp
        from app.gui.model_manager import find_embedded_llm_path
        from app.providers.llama_cpp_provider import LlamaCppClient

        llm_path = find_embedded_llm_path()
        if llm_path is not None:
            client = LlamaCppClient(model_path=llm_path)
            if client.is_available():
                logger_fn(
                    f"Summary backend: bundled LLM ({llm_path.name})."
                )
                return Summarizer(
                    options=options, llm_client=client, logger_fn=logger_fn
                )
            logger_fn(
                "Embedded LLM file present but llama-cpp-python is not "
                "loadable; falling through to extractive."
            )

        # 3) Extractive
        logger_fn(
            "No LLM backend available — using offline extractive "
            "summarizer (sentence picker, not abstractive)."
        )
        return Summarizer(options=options, llm_client=None, logger_fn=logger_fn)

    def clean_writer(self, logger_fn) -> CleanTranscriptWriter:
        if self._clean_writer is None:
            self._clean_writer = CleanTranscriptWriter(logger_fn=logger_fn)
        else:
            self._clean_writer._logger = logger_fn  # noqa: SLF001
        return self._clean_writer

    def summary_writer(self) -> SummaryWriter:
        if self._summary_writer is None:
            self._summary_writer = SummaryWriter()
        return self._summary_writer


class TranscriptionWorker(QObject):
    """Owns the queue and the QThread that drains it.

    Signals are emitted from the worker thread; the main window connects
    them via ``Qt.QueuedConnection`` (the default for cross-thread signals)
    so the UI updates happen on the GUI thread.
    """

    job_started = Signal(int)
    job_progress = Signal(int, float)  # job_id, fraction in [0, 1]
    job_log = Signal(int, str)
    job_finished = Signal(int, str)  # job_id, status name
    queue_drained = Signal()

    def __init__(self) -> None:
        # IMPORTANT: do NOT pass a parent QObject here. We immediately
        # move ``self`` into a new QThread; if the worker also has a
        # parent that lives on a different thread, Qt prints
        # "QObject::moveToThread: Current parent is in a different
        # thread" and the signal-slot delivery to the main thread can
        # stall (causing Windows DWM to flag the main window as
        # "Not Responding" even though the event loop is running).
        super().__init__(None)
        self._jobs: list[Job] = []
        self._next_id = 1
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._cancel_all = threading.Event()
        self._stop = threading.Event()
        self._thread = QThread()
        self.moveToThread(self._thread)
        self._thread.started.connect(self._run)
        self._services = _PipelineServices()
        self._current_job_id: Optional[int] = None

    # ---- Public API (called from GUI thread) -----------------------------------

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._cancel_all.set()
        self._wakeup.set()
        with self._lock:
            for job in self._jobs:
                if job.status in (JobStatus.QUEUED, JobStatus.PROCESSING):
                    job.cancel_event.set()
        self._thread.quit()
        self._thread.wait(5000)

    def add_files(
        self,
        paths: list[Path],
        mode: JobMode = JobMode.BOTH,
    ) -> list[Job]:
        added: list[Job] = []
        with self._lock:
            for p in paths:
                job = Job(job_id=self._next_id, file_path=Path(p), mode=mode)
                self._next_id += 1
                self._jobs.append(job)
                added.append(job)
        self._wakeup.set()
        return added

    def cancel_job(self, job_id: int) -> None:
        with self._lock:
            for job in self._jobs:
                if job.job_id != job_id:
                    continue
                if job.status == JobStatus.QUEUED:
                    job.status = JobStatus.CANCELLED
                    self.job_finished.emit(job.job_id, job.status.value)
                elif job.status == JobStatus.PROCESSING:
                    job.cancel_event.set()
                return

    def cancel_all(self) -> None:
        self._cancel_all.set()
        with self._lock:
            for job in self._jobs:
                if job.status == JobStatus.QUEUED:
                    job.status = JobStatus.CANCELLED
                    self.job_finished.emit(job.job_id, job.status.value)
                elif job.status == JobStatus.PROCESSING:
                    job.cancel_event.set()
        self._wakeup.set()

    def snapshot(self) -> list[Job]:
        with self._lock:
            return list(self._jobs)

    # ---- Worker thread loop ----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self._next_job()
            if job is None:
                # Nothing to do — wait for a signal.
                if self._cancel_all.is_set():
                    self._cancel_all.clear()
                self.queue_drained.emit()
                self._wakeup.wait(timeout=0.5)
                self._wakeup.clear()
                continue
            self._process_job(job)

    def _next_job(self) -> Optional[Job]:
        with self._lock:
            for job in self._jobs:
                if job.status == JobStatus.QUEUED:
                    job.status = JobStatus.PROCESSING
                    self._current_job_id = job.job_id
                    return job
        self._current_job_id = None
        return None

    def _process_job(self, job: Job) -> None:
        self.job_started.emit(job.job_id)
        _file_log(f"job {job.job_id}: started ({job.file_path})")

        def _log(msg: str) -> None:
            self.job_log.emit(job.job_id, msg)
            _file_log(f"job {job.job_id}: {msg}")

        def _progress(fraction: float) -> None:
            job.progress = max(0.0, min(1.0, fraction))
            self.job_progress.emit(job.job_id, job.progress)

        try:
            # Map the job mode onto the pipeline knobs.
            summary_backend = "ollama" if job.mode != JobMode.TRANSCRIPTION else "none"
            write_clean = job.mode != JobMode.SUMMARY
            write_summary = job.mode != JobMode.TRANSCRIPTION

            options = TranscribeOptions(
                profile="best",
                language=None,  # auto-detect
                timeout_sec=0,
                include_chapters=True,
                summary=SummaryOptions(mode=summary_backend),
            )
            # Prefer the bundled flat-layout model dir when present; fall
            # back to the short name "large-v3" only in dev mode without
            # a populated models/ dir (which then errors loudly via the
            # provider's local_files_only=True guard).
            embedded_path = find_embedded_model_path()
            model_identifier = (
                str(embedded_path) if embedded_path is not None
                else embedded_model_name()
            )
            result = run_pipeline(
                video_path=job.file_path,
                options=options,
                output_dir=None,
                title_style="keywords",
                clean_mode="rule-based",
                write_clean_file=write_clean,
                write_summary_file=write_summary,
                extractor=self._services.extractor(),
                transcriber=self._services.transcriber(_log),
                summarizer=self._services.summarizer(_log),
                clean_writer=self._services.clean_writer(_log),
                summary_writer=self._services.summary_writer(),
                progress_fn=_progress,
                logger_fn=_log,
                model_name=model_identifier,
                cancel_event=job.cancel_event,
            )
            job.transcript_path = result.transcript_path
            job.summary_path = result.summary_path
            if job.cancel_event.is_set():
                job.status = JobStatus.CANCELLED
            else:
                job.status = JobStatus.DONE
        except Exception as exc:  # noqa: BLE001
            if job.cancel_event.is_set():
                job.status = JobStatus.CANCELLED
            else:
                job.status = JobStatus.FAILED
                job.error_message = str(exc) or exc.__class__.__name__
                _log(f"ERROR: {job.error_message}")
                # Keep the traceback in the logs for debugging, but the UI
                # only shows the message.
                _log(traceback.format_exc())
                _file_log(
                    f"job {job.job_id}: traceback:\n{traceback.format_exc()}",
                    level="ERROR",
                )

        _file_log(f"job {job.job_id}: finished status={job.status.value}")
        self.job_finished.emit(job.job_id, job.status.value)
