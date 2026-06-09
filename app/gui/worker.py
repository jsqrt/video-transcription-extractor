"""Background worker that processes one file at a time.

Lives on its own QThread so the Qt event loop stays responsive while
Whisper crunches audio. Owns one cancel event per active job so the UI
can either cancel the *current* file (skip to next) or the *entire*
queue (drain everything, set every pending job's status to ``cancelled``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from app.gui.app_logger import log as _file_log
from app.gui.model_manager import (
    embedded_model_name,
    find_embedded_model_path,
    find_embedded_whisper_ggml_path,
)
from app.models.types import SummaryOptions, TranscribeOptions
from app.providers.faster_whisper_provider import FasterWhisperProvider
from app.services.audio_extractor import AudioExtractor
from app.services.pipeline import run_pipeline
from app.services.summarizer import Summarizer
from app.services.summary_writer import SummaryWriter
from app.services.transcriber import Transcriber
from app.services.writer import CleanTranscriptWriter


# Fraction of a both-artifacts job's progress bar allocated to
# transcription; the remainder is summarisation. We weight the LLM stage
# generously because on a 22-minute Ukrainian news bulletin map-reduce
# summarisation on qwen2.5:7b takes ~8 minutes versus Whisper's ~4 — the
# old 85/15 split made the bar appear frozen for the entire summary
# stage. 40/60 means short clips show a brief catch-up jump (ASR done at
# 40 %, summary completes in 15-30 s) and long clips animate smoothly
# from start to finish.
_TRANSCRIBE_SHARE = 0.4


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


class TranscribeMode(str, Enum):
    """Quality / speed trade-off for a single job.

    QUALITY routes the job through faster-whisper (CTranslate2), which
    enables ``multilingual=True`` — per-segment language detection that
    keeps English loanwords ("batch", "prompt", "wireframe") in
    English script instead of "translating" them into a phonetically
    similar Ukrainian word. SPEED keeps whisper.cpp (Metal / Vulkan on
    accelerated platforms, CPU otherwise), which is monolingual but
    significantly faster on Apple Silicon.

    The split is per-job rather than global because users routinely mix
    pure-Ukrainian recordings (where SPEED is fine) with bilingual
    UK/EN recordings (where QUALITY is needed).
    """

    QUALITY = "quality"
    SPEED = "speed"


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
    # When the transcription finishes but the summary stage failed
    # separately (Ollama down, timeout, model OOM…), the pipeline
    # returns a transcript_path and a summary_error rather than raising.
    # We propagate that string up so the UI can surface a non-fatal
    # warning instead of marking the whole job DONE silently.
    summary_error: Optional[str] = None
    mode: JobMode = JobMode.BOTH
    # Speed (default) → whisper.cpp on the accelerated path (Metal on
    # macOS, Vulkan/CUDA on Win/Linux), monolingual so loanwords may get
    # "translated". Quality → faster-whisper with multilingual decoding so
    # English loanwords stay in English; the user opts into it explicitly.
    transcribe_mode: TranscribeMode = TranscribeMode.SPEED
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # Force a specific language for both Whisper and the summarizer
    # prompt. Used by the right-click "Re-summarize as <Language>"
    # action: the original auto-detect mis-classified the audio and
    # the user is overriding it. ``None`` = auto-detect (default).
    forced_language: Optional[str] = None
    # ISO-639-1 code Whisper actually detected on the last successful
    # run for this file. Surfaced as a chip in the UI so the user can
    # see WHY their summary came out in a given language.
    detected_language: Optional[str] = None
    detected_language_probability: Optional[float] = None


class _PipelineServices:
    """Built once and reused across the queue so each Whisper backend is
    loaded only on the first job that needs it and cached afterwards.

    Two transcribers are tracked side-by-side, one per
    :class:`TranscribeMode`. Both are built lazily on the worker thread
    because importing faster-whisper / whisper.cpp is expensive.
    """

    def __init__(self) -> None:
        # Keyed by TranscribeMode. ``provider`` is the live
        # FasterWhisperProvider / WhisperCppProvider; ``kind`` is the
        # label the caller uses to pick the model-identifier shape
        # (CT2 directory vs GGML .bin path).
        self._providers: dict[TranscribeMode, object] = {}
        self._provider_kinds: dict[TranscribeMode, str] = {}
        self._transcribers: dict[TranscribeMode, Transcriber] = {}
        self._summary_writer: Optional[SummaryWriter] = None
        self._clean_writer: Optional[CleanTranscriptWriter] = None
        self._extractor: Optional[AudioExtractor] = None

    def transcriber(
        self,
        logger_fn,
        transcribe_mode: TranscribeMode = TranscribeMode.SPEED,
    ) -> Transcriber:
        existing = self._transcribers.get(transcribe_mode)
        if existing is not None:
            # Keep the latest job's logger so per-file warnings reach the UI.
            existing._logger = logger_fn  # noqa: SLF001
            return existing

        provider, kind = self._build_provider(logger_fn, transcribe_mode)
        transcriber = Transcriber(provider=provider, logger_fn=logger_fn)
        self._providers[transcribe_mode] = provider
        self._provider_kinds[transcribe_mode] = kind
        self._transcribers[transcribe_mode] = transcriber
        return transcriber

    def provider_kind(
        self,
        transcribe_mode: TranscribeMode = TranscribeMode.SPEED,
    ) -> Optional[str]:
        """Tell the caller which ASR backend got picked for ``transcribe_mode``.

        Used to route the correct model identifier into the transcriber
        (CT2 directory vs GGML .bin file). ``None`` until the first
        ``transcriber(transcribe_mode=…)`` call for that mode.
        """
        return self._provider_kinds.get(transcribe_mode)

    def _build_provider(self, logger_fn, transcribe_mode: TranscribeMode):
        """Pick an ASR backend for ``transcribe_mode``.

        Quality always routes to faster-whisper (CTranslate2). That
        engine is the only path that supports ``multilingual=True`` —
        per-segment language detection — which is what lets English
        loanwords ("batch", "prompt") stay in English instead of being
        re-spelled into a phonetically-similar Ukrainian word. On
        platforms without CUDA / Metal acceleration for CT2 (macOS,
        Windows without NVIDIA) Quality runs on CPU and is noticeably
        slower; that's the price the user accepts when picking it.

        Speed picks the fastest accelerated backend available:

        ============  =============  =========================================
        Platform      GPU vendor     Speed backend
        ============  =============  =========================================
        macOS (any)   —              whisper.cpp (Metal/Accelerate)
        Windows       NVIDIA         faster-whisper (CUDA)
        Windows       AMD / Intel    whisper.cpp (Vulkan, if bundled)
        Windows       none           faster-whisper CPU
        Linux         (same as Windows)
        ============  =============  =========================================

        ``DESCRIBELY_ASR_BACKEND=whisper-cpp / =faster-whisper`` still
        forces the backend globally (support escape hatch). When set,
        BOTH Quality and Speed honour it — same as before the per-job
        mode existed — so existing support workflows keep working.
        """
        override = (os.environ.get("DESCRIBELY_ASR_BACKEND") or "").strip().lower()

        if override not in ("", "whisper-cpp", "faster-whisper"):
            raise RuntimeError(
                f"Unknown DESCRIBELY_ASR_BACKEND={override!r}. "
                "Use 'whisper-cpp' or 'faster-whisper'."
            )

        prefer_whisper_cpp = self._prefer_whisper_cpp(
            override, transcribe_mode, logger_fn
        )

        if prefer_whisper_cpp:
            from app.providers.whisper_cpp_provider import (
                WhisperCppProvider,
                pywhispercpp_available,
            )
            if pywhispercpp_available():
                backend_label = (
                    "Metal/Accelerate" if sys.platform == "darwin" else "Vulkan"
                )
                logger_fn(
                    f"ASR backend ({transcribe_mode.value}): "
                    f"whisper.cpp ({backend_label})."
                )
                return WhisperCppProvider(), "whisper-cpp"
            if override == "whisper-cpp":
                raise RuntimeError(
                    "DESCRIBELY_ASR_BACKEND=whisper-cpp but pywhispercpp "
                    "is not importable. Install it or unset the override."
                )
            logger_fn(
                "pywhispercpp unavailable in this build — falling back "
                "to faster-whisper (CPU on non-NVIDIA hosts)."
            )

        logger_fn(f"ASR backend ({transcribe_mode.value}): faster-whisper.")
        return FasterWhisperProvider(), "faster-whisper"

    @staticmethod
    def _prefer_whisper_cpp(
        override: str,
        transcribe_mode: TranscribeMode,
        logger_fn,
    ) -> bool:
        if override == "whisper-cpp":
            return True
        if override == "faster-whisper":
            return False
        # Quality mode is the whole point of the per-job switch: it
        # exists to opt into faster-whisper's multilingual decoder. So
        # we route it to faster-whisper unconditionally, regardless of
        # platform — even on macOS where CT2 has no Metal and we pay a
        # CPU-speed penalty for it.
        if transcribe_mode == TranscribeMode.QUALITY:
            return False
        # Speed mode picks the fastest accelerated backend. macOS only
        # has Metal via whisper.cpp; Windows/Linux pick whisper.cpp
        # only when no NVIDIA card is available (faster-whisper CUDA
        # is the fastest path there).
        if sys.platform == "darwin":
            return True
        from app.gui.gpu_detect import has_nvidia_gpu
        if has_nvidia_gpu():
            return False
        return True

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
            # The summary model is FIXED to options.ollama_model
            # (qwen2.5:7b) — the standalone-chunk → concatenate pipeline is
            # run on it with small (~450-token) chunks so the model stays
            # inside its working context window; each chunk's summary is
            # streamed to the .summary.md file as it is produced. There is no
            # "best available" fallback: if the
            # exact model is not installed we do NOT silently downgrade. We
            # fall through to the bundled LLM / extractive path and tell the
            # user how to get the real summary back.
            if options.ollama_model in installed_models:
                ollama = OllamaClient(
                    base_url=options.ollama_base_url,
                    model=options.ollama_model,
                    timeout_sec=options.ollama_timeout_sec,
                )
                logger_fn(f"Summary backend: Ollama ({options.ollama_model}).")
                return Summarizer(
                    options=options, llm_client=ollama, logger_fn=logger_fn
                )
            logger_fn(
                f"Ollama is running but the required summary model "
                f"'{options.ollama_model}' is not installed — install it "
                f"with `ollama pull {options.ollama_model}`. Falling back "
                f"to the bundled summarizer for now."
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
    # Emitted whenever pause state changes. The GUI flips the toolbar
    # button label / icon in response.
    pause_state_changed = Signal(bool)

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
        # When set, the worker loop stops PICKING new queued jobs but
        # never interrupts the one already running — pause is "after
        # current file", not "right now". Resume just clears the flag
        # and wakes the loop. Set from the GUI thread, read from the
        # worker thread; the GIL plus the trip through ``_wakeup``
        # makes a plain Event the simplest correct primitive here.
        self._paused = threading.Event()
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
        forced_language: Optional[str] = None,
        transcribe_mode: TranscribeMode = TranscribeMode.SPEED,
    ) -> list[Job]:
        added: list[Job] = []
        with self._lock:
            for p in paths:
                job = Job(
                    job_id=self._next_id,
                    file_path=Path(p),
                    mode=mode,
                    forced_language=forced_language,
                    transcribe_mode=transcribe_mode,
                )
                self._next_id += 1
                self._jobs.append(job)
                added.append(job)
        self._wakeup.set()
        return added

    def set_transcribe_mode(self, job_id: int, transcribe_mode: TranscribeMode) -> bool:
        """Update the per-job transcribe mode while it's still queued.

        Returns True if the mode was changed; False if the job is
        already running, done, or missing. The UI uses the return
        value to roll back the combo-box selection on race conditions
        (e.g. the worker picked the job up between the user's click
        and Qt delivering the signal).
        """
        with self._lock:
            for job in self._jobs:
                if job.job_id != job_id:
                    continue
                if job.status != JobStatus.QUEUED:
                    return False
                job.transcribe_mode = transcribe_mode
                return True
        return False

    def set_paused(self, paused: bool) -> None:
        """Pause / resume the queue. Pause is cooperative: the file
        currently being processed runs to completion, but no new jobs
        are picked up until ``set_paused(False)`` is called."""
        was_paused = self._paused.is_set()
        if paused:
            self._paused.set()
        else:
            self._paused.clear()
            self._wakeup.set()
        if paused != was_paused:
            self.pause_state_changed.emit(paused)

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def reorder_queued(self, job_ids: list[int]) -> None:
        """Apply a user-driven reorder of QUEUED jobs.

        ``job_ids`` is the desired order of currently queued ids. Jobs
        not in QUEUED state (running, done, cancelled) keep their
        relative positions and never move — reordering them would
        either be meaningless (already finished) or unsafe (already
        executing). Unknown ids are silently dropped."""
        with self._lock:
            queued = {j.job_id: j for j in self._jobs if j.status == JobStatus.QUEUED}
            new_queued_order: list[Job] = []
            seen: set[int] = set()
            for jid in job_ids:
                job = queued.get(jid)
                if job is None or jid in seen:
                    continue
                seen.add(jid)
                new_queued_order.append(job)
            # Any queued job the caller forgot to mention keeps its
            # tail position rather than disappearing.
            for job in self._jobs:
                if job.status == JobStatus.QUEUED and job.job_id not in seen:
                    new_queued_order.append(job)

            iterator = iter(new_queued_order)
            rebuilt: list[Job] = []
            for job in self._jobs:
                if job.status == JobStatus.QUEUED:
                    rebuilt.append(next(iterator))
                else:
                    rebuilt.append(job)
            self._jobs = rebuilt

    def remove_finished(self) -> list[int]:
        """Drop every DONE / FAILED / CANCELLED job from the queue.

        Returns the removed job ids so the GUI can take down the
        corresponding rows in one pass."""
        terminal = {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}
        removed: list[int] = []
        with self._lock:
            keep: list[Job] = []
            for job in self._jobs:
                if job.status in terminal:
                    removed.append(job.job_id)
                else:
                    keep.append(job)
            self._jobs = keep
        return removed

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

    def find_job(self, job_id: int) -> Optional[Job]:
        """Return the live Job for ``job_id`` or None.

        Returned reference is the actual queue entry, so any read of
        its fields is up-to-date the instant the lock is released. The
        UI uses this for rollback after a rejected mode change.
        """
        with self._lock:
            for job in self._jobs:
                if job.job_id == job_id:
                    return job
        return None

    # ---- Worker thread loop ----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            # While paused, sleep on the wakeup event but DO NOT pick
            # a job. ``set_paused(False)`` sets ``_wakeup`` so we exit
            # this branch promptly.
            if self._paused.is_set():
                self._wakeup.wait(timeout=0.5)
                self._wakeup.clear()
                continue
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

        # When a job produces a summary, transcription owns the first
        # ``_TRANSCRIBE_SHARE`` of the bar and summarisation the rest, so
        # the user sees the bar keep advancing through the LLM stage
        # instead of sitting at 100 % while Ollama works. Transcription-
        # only jobs let transcription fill the whole bar.
        produces_summary = job.mode != JobMode.TRANSCRIPTION
        transcribe_share = _TRANSCRIBE_SHARE if produces_summary else 1.0

        def _emit(fraction: float) -> None:
            job.progress = max(0.0, min(1.0, fraction))
            self.job_progress.emit(job.job_id, job.progress)

        def _progress(fraction: float) -> None:
            _emit(fraction * transcribe_share)

        def _summary_progress(fraction: float) -> None:
            _emit(transcribe_share + fraction * (1.0 - transcribe_share))

        try:
            # Map the job mode onto the pipeline knobs. The transcription
            # file is always written; the summary file is added on top
            # when the user picked BOTH.
            summary_backend = "ollama" if job.mode == JobMode.BOTH else "none"
            write_clean = True
            write_summary = job.mode == JobMode.BOTH

            # The GUI has no dedicated prompt field yet, but power users
            # can pre-load a domain vocabulary via env var without code
            # changes (e.g. DESCRIBELY_INITIAL_PROMPT="пейдж, фронтенд,
            # бекенд, деплой"). Empty / unset leaves Whisper to its
            # no-prompt default.
            initial_prompt = (os.environ.get("DESCRIBELY_INITIAL_PROMPT") or "").strip() or None
            options = TranscribeOptions(
                profile="best",
                # ``forced_language`` overrides auto-detect — set by
                # the right-click "Re-summarize as <Language>" path
                # when the user is correcting a mis-detection.
                language=job.forced_language,
                timeout_sec=0,
                summary=SummaryOptions(mode=summary_backend),
                initial_prompt=initial_prompt,
            )
            # Construct the transcriber first so provider_kind is known,
            # then pick the model identifier shape that backend expects:
            # GGML .bin file for whisper.cpp, CT2 directory for
            # faster-whisper. The per-job ``transcribe_mode`` is what
            # routes us to the right backend cache slot.
            transcriber = self._services.transcriber(_log, job.transcribe_mode)
            if self._services.provider_kind(job.transcribe_mode) == "whisper-cpp":
                ggml_path = find_embedded_whisper_ggml_path()
                if ggml_path is None:
                    raise RuntimeError(
                        "macOS build expects models/whisper-ggml/"
                        "ggml-large-v3-turbo-q5_0.bin but it was not "
                        "found. Run scripts/fetch_whisper_ggml.py "
                        "--variant large-v3-turbo-q5_0 before building."
                    )
                model_identifier = str(ggml_path)
            else:
                embedded_path = find_embedded_model_path()
                model_identifier = (
                    str(embedded_path) if embedded_path is not None
                    else embedded_model_name()
                )

            result = run_pipeline(
                video_path=job.file_path,
                options=options,
                output_dir=None,
                clean_mode="rule-based",
                write_clean_file=write_clean,
                write_summary_file=write_summary,
                extractor=self._services.extractor(),
                transcriber=transcriber,
                summarizer=self._services.summarizer(_log),
                clean_writer=self._services.clean_writer(_log),
                summary_writer=self._services.summary_writer(),
                progress_fn=_progress,
                summary_progress_fn=_summary_progress,
                logger_fn=_log,
                model_name=model_identifier,
                cancel_event=job.cancel_event,
            )
            job.transcript_path = result.transcript_path
            job.summary_path = result.summary_path
            job.summary_error = result.summary_error
            job.detected_language = result.detected_language
            job.detected_language_probability = result.detected_language_probability
            if job.cancel_event.is_set():
                job.status = JobStatus.CANCELLED
            else:
                job.status = JobStatus.DONE
                # The transcript landed but the summary stage failed
                # cleanly inside the pipeline (it doesn't re-raise so
                # the .transcription.md file still gets written). Mark
                # the job as DONE but stash the reason so the row can
                # show a hover warning instead of looking like the
                # summary just silently never happened.
                if (
                    job.mode == JobMode.BOTH
                    and result.summary_path is None
                    and result.summary_error
                ):
                    job.error_message = f"Summary failed: {result.summary_error}"
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
