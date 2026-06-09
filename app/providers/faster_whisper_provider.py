from __future__ import annotations

import os
import queue
import re
import sys
import threading
import wave
from pathlib import Path
from typing import Callable, Iterable

from app.models.types import (
    ModelNotFoundError,
    TranscriptionError,
    TranscriptionTimeoutError,
)

# Exception message fragments that tell us CUDA is unusable on this host.
_CUDA_ERROR_FRAGMENTS = (
    "cublas",
    "cudnn",
    "cuda",
    "libcublas",
    "libcudnn",
    "no cuda",
    "cuda driver",
    "unknown error",  # ctranslate2 often reports plain "CUDA: unknown error"
)


def _looks_like_cuda_failure(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(fragment in message for fragment in _CUDA_ERROR_FRAGMENTS)


def _platform_prefers_gpu_by_default() -> bool:
    """macOS has no CUDA path — skip the GPU probe entirely there."""
    return sys.platform != "darwin"


class FasterWhisperProvider:
    """Local Whisper inference via ``faster-whisper`` with optional GPU."""

    def __init__(
        self,
        model_cache_dir: Path | None = None,
        prefer_gpu: bool | None = None,
    ) -> None:
        self._model_cache: dict[str, object] = {}
        self.model_cache_dir = model_cache_dir
        self.prefer_gpu = (
            _platform_prefers_gpu_by_default() if prefer_gpu is None else prefer_gpu
        )
        # Populated by the last call to ``transcribe`` so the pipeline
        # can route the detected language into the summarizer prompt.
        # ``None`` when the user forced a language or detection failed.
        self.last_detected_language: str | None = None
        self.last_detected_language_probability: float | None = None

    # ---- CUDA runtime discovery -------------------------------------------------

    def _configure_cuda_runtime(self) -> None:
        """Best-effort CUDA shared-library discovery on Windows and Linux.

        Two modes:

        * Frozen build (PyInstaller): only the bundle's own
          ``_internal/nvidia/<sub>/bin/`` directories are added to the
          DLL search path. We intentionally do NOT scan ``sys.prefix``,
          which in a PyInstaller layout points at the bundle root and
          would let an attacker with write access to a sibling folder
          plant a fake ``cublas.dll``.
        * Dev build (running from a venv): full scan via
          ``_cuda_library_candidates`` covers wheels installed in
          ``site-packages/nvidia/``.

        Without this, ``CTranslate2`` cannot ``LoadLibrary("cublas64_12.dll")``
        from the bundled NVIDIA wheel and silently falls back to CPU.
        """
        if sys.platform == "darwin":
            return
        if getattr(sys, "frozen", False):
            self._register_bundled_cuda_dirs()
            return
        candidates = self._cuda_library_candidates()
        for candidate in candidates:
            resolved = str(candidate)
            if hasattr(os, "add_dll_directory") and os.name == "nt":
                try:
                    os.add_dll_directory(resolved)
                except OSError:
                    pass
            if os.name == "nt":
                self._prepend_path_env("PATH", resolved)
            else:
                self._prepend_path_env("LD_LIBRARY_PATH", resolved)

    def _register_bundled_cuda_dirs(self) -> None:
        """Register the frozen bundle's own CUDA wheel ``bin/`` dirs.

        PyInstaller --onedir places ``nvidia-cublas-cu12`` and
        ``nvidia-cudnn-cu12`` wheels under
        ``<bundle>/_internal/nvidia/<pkg>/``. The DLLs live in the
        ``bin/`` subdirectory of each. Windows' ``LoadLibrary`` only
        searches the launcher's own directory plus system paths by
        default — we have to opt them in explicitly via
        ``os.add_dll_directory``. Safe because we only scan paths
        inside the install root, never user-writable locations.
        """
        bundle_root = Path(sys.executable).resolve().parent
        # In --onedir, deps live under _internal/. In --onefile they
        # are extracted to sys._MEIPASS. Handle both for completeness.
        roots = [bundle_root / "_internal", bundle_root]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.insert(0, Path(meipass))

        for root in roots:
            nvidia_dir = root / "nvidia"
            if not nvidia_dir.is_dir():
                continue
            for sub in nvidia_dir.iterdir():
                bin_dir = sub / "bin"
                if not bin_dir.is_dir():
                    continue
                resolved = str(bin_dir.resolve())
                if hasattr(os, "add_dll_directory") and os.name == "nt":
                    try:
                        os.add_dll_directory(resolved)
                    except OSError:
                        pass
                if os.name == "nt":
                    self._prepend_path_env("PATH", resolved)
                else:
                    self._prepend_path_env("LD_LIBRARY_PATH", resolved)

    def _cuda_library_candidates(self) -> list[Path]:
        seen: set[str] = set()
        candidates: list[Path] = []

        def _add(path: Path) -> None:
            try:
                resolved = path.resolve()
            except OSError:
                return
            key = str(resolved).lower()
            if key in seen:
                return
            if not resolved.exists():
                return
            seen.add(key)
            candidates.append(resolved)

        cuda_path = os.environ.get("CUDA_PATH")
        if cuda_path:
            _add(Path(cuda_path) / ("bin" if os.name == "nt" else "lib64"))

        # Locate NVIDIA wheels placed inside the active interpreter.
        site_roots: list[Path] = []
        if os.name == "nt":
            site_roots.append(Path(sys.prefix) / "Lib" / "site-packages" / "nvidia")
        else:
            lib_dir = Path(sys.prefix) / "lib"
            if lib_dir.exists():
                for python_dir in lib_dir.glob("python*"):
                    site_roots.append(python_dir / "site-packages" / "nvidia")
            site_roots.append(Path(sys.prefix) / "lib" / "site-packages" / "nvidia")

        for site in site_roots:
            if not site.exists():
                continue
            for sub in site.iterdir():
                _add(sub / ("bin" if os.name == "nt" else "lib"))

        return candidates

    @staticmethod
    def _prepend_path_env(var: str, value: str) -> None:
        current = os.environ.get(var, "")
        parts = current.split(os.pathsep) if current else []
        if value in parts:
            return
        os.environ[var] = value + (os.pathsep + current if current else "")

    # ---- Model loading ----------------------------------------------------------

    def _import_whisper_model(self):
        try:
            from faster_whisper import WhisperModel  # noqa: WPS433 (import here by design)
        except ImportError as exc:
            raise TranscriptionError(
                "faster-whisper is not installed. Install dependencies: "
                "pip install -r requirements.txt"
            ) from exc
        return WhisperModel

    def _load_model(self, model_name: str, prefer_gpu: bool):
        cache_key = f"{model_name}:{'gpu' if prefer_gpu else 'cpu'}"
        if cache_key in self._model_cache:
            return self._model_cache[cache_key]

        whisper_cls = self._import_whisper_model()
        self._configure_cuda_runtime()

        plan: list[tuple[str, str]]
        if prefer_gpu:
            plan = [("cuda", "float16"), ("cpu", "int8")]
        else:
            plan = [("cpu", "int8")]

        last_exc: Exception | None = None
        for device, compute_type in plan:
            try:
                model = whisper_cls(
                    model_name,
                    device=device,
                    compute_type=compute_type,
                    download_root=str(self.model_cache_dir) if self.model_cache_dir else None,
                    local_files_only=True,
                )
                self._model_cache[cache_key] = model
                return model
            except Exception as exc:
                last_exc = exc
                # Any failure on the GPU attempt (CUDA-specific or not) falls
                # through to the next plan entry (CPU); ``last_exc`` carries
                # the reason into the final error message below.

        message = str(last_exc) if last_exc else "unknown error"
        if "not found" in message.lower() or "repository" in message.lower() or "no such file" in message.lower():
            raise ModelNotFoundError(
                f"Whisper model '{model_name}' is not available locally. "
                "Pre-seed the model cache or try: tiny, base, small, medium, large-v3"
            ) from last_exc
        raise TranscriptionError(f"Failed to load Whisper model: {message}") from last_exc

    # ---- Helpers ----------------------------------------------------------------

    @staticmethod
    def _read_wav_duration_seconds(audio_path: Path) -> float | None:
        try:
            with wave.open(str(audio_path), "rb") as wav_file:
                rate = wav_file.getframerate()
                if rate <= 0:
                    return None
                return wav_file.getnframes() / float(rate)
        except (OSError, wave.Error):
            return None

    def _collect_lines_with_progress(
        self,
        segments: Iterable[object],
        audio_duration_seconds: float | None,
        progress_callback: Callable[[float], None] | None,
        cancel_event: threading.Event | None,
    ) -> list[str]:
        lines: list[str] = []
        last_reported_percent = -1
        last_normalized_text = ""
        duplicate_streak = 0
        max_duplicate_streak = 8

        for segment in segments:
            if cancel_event is not None and cancel_event.is_set():
                raise TranscriptionTimeoutError("Transcription cancelled")

            text = (getattr(segment, "text", "") or "").strip()
            if text:
                normalized_text = re.sub(r"\s+", " ", text).strip().lower()
                if normalized_text == last_normalized_text:
                    duplicate_streak += 1
                else:
                    duplicate_streak = 0
                    last_normalized_text = normalized_text

                if duplicate_streak <= max_duplicate_streak:
                    segment_start = float(getattr(segment, "start", 0.0) or 0.0)
                    segment_end = float(getattr(segment, "end", segment_start) or segment_start)
                    if segment_end < segment_start:
                        segment_start, segment_end = segment_end, segment_start
                    lines.append(
                        f"[{segment_start:.2f} -> {segment_end:.2f}] [UNKNOWN_SPEAKER] {text}"
                    )

            if progress_callback and audio_duration_seconds and audio_duration_seconds > 0:
                segment_end = float(getattr(segment, "end", 0.0) or 0.0)
                progress = max(0.0, min(1.0, segment_end / audio_duration_seconds))
                percent = int(progress * 100)
                if percent > last_reported_percent:
                    last_reported_percent = percent
                    progress_callback(progress)

        if progress_callback:
            progress_callback(1.0)

        return lines

    def _build_transcribe_kwargs(
        self,
        profile: str,
        language: str | None,
        initial_prompt: str | None = None,
    ) -> dict[str, object]:
        normalized_profile = (profile or "best").lower()
        kwargs: dict[str, object] = {
            "vad_filter": True,
            "condition_on_previous_text": False,
            "compression_ratio_threshold": 2.2,
            "log_prob_threshold": -1.0,
            "no_speech_threshold": 0.6,
        }
        if language:
            # User-forced monolingual decoding: trust them, no per-segment
            # detection (they've decided English loanwords should be
            # re-written into the forced script).
            kwargs["language"] = language
        else:
            # Mixed-language mode: let faster-whisper detect language on
            # every segment. Without this flag the library locks the
            # language to whatever it sees in the first 30 s and
            # "translates" English loanwords ("пейдж", "фронтенд") into
            # phonetically-similar Ukrainian words for the rest of the
            # file. Multilingual mode lets an English chunk inside
            # Ukrainian speech stay in English script.
            kwargs["multilingual"] = True
            # Raise the per-segment switch threshold well above the 0.5
            # default. With the default, a Ukrainian sentence that
            # phonetically resembles Czech ("дякую за увагу" → "děkuji
            # za ovláhu") can flip an entire segment to Czech with only
            # 51% confidence. The 0.8 cut-off says: only switch script
            # when the model is genuinely sure, otherwise stay in the
            # previous (correct) language. Catches the EN-loanword case
            # (those tend to be 0.9+ confident) without false-positive
            # flips into related Slavic / Turkic languages.
            kwargs["language_detection_threshold"] = 0.8
        if normalized_profile == "fast":
            kwargs.update({"beam_size": 2, "best_of": 2, "temperature": 0.0})
        else:
            # beam_size=5/best_of=5 are the reference OpenAI settings,
            # but on macOS faster-whisper runs on CPU only (no Metal in
            # CTranslate2). That turned a 22-minute Ukrainian bulletin
            # into a 30-minute Quality run — too slow for the GUI to be
            # useful. beam_size=3/best_of=3 keeps Quality on the CPU
            # path comfortably under 15 minutes for the same file with
            # only a small WER hit (low single-digit % on Ukrainian).
            kwargs.update({"beam_size": 3, "best_of": 3, "temperature": 0.0})
        prompt_text = (initial_prompt or "").strip()
        if prompt_text:
            kwargs["initial_prompt"] = prompt_text
        return kwargs

    def _record_detected_language(self, info: object, *, forced: bool) -> None:
        """Capture the language Whisper auto-detected on this run.

        ``info`` is the second return value of ``faster_whisper``'s
        ``transcribe`` — a ``TranscriptionInfo`` carrying ``language``
        (ISO-639-1) and ``language_probability``. When the user forced
        a language we leave detection empty so the pipeline knows the
        value isn't an actual model decision."""
        if forced:
            return
        language = getattr(info, "language", None)
        if not language:
            return
        code = str(language).strip().lower()
        if not code:
            return
        probability = getattr(info, "language_probability", None)
        try:
            probability_f = float(probability) if probability is not None else None
        except (TypeError, ValueError):
            probability_f = None
        self.last_detected_language = code
        self.last_detected_language_probability = probability_f

    def release(self) -> None:
        """Delete all cached Whisper models and free GPU memory.

        Call this after transcription is complete so Ollama (or any other
        process) can claim the VRAM for its own inference.
        """
        self._model_cache.clear()
        # Ask Python to collect the now-unreferenced model objects immediately
        # so ctranslate2 releases cuBLAS/cuDNN handles before we return.
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    # ---- Public API -------------------------------------------------------------

    def transcribe(
        self,
        audio_path: Path,
        model: str,
        profile: str,
        language: str | None,
        timeout_sec: int,
        progress_callback: Callable[[float], None] | None = None,
        cancel_event: threading.Event | None = None,
        initial_prompt: str | None = None,
    ) -> str:
        # Reset detection state — each call must reflect THIS file only.
        self.last_detected_language = None
        self.last_detected_language_probability = None

        audio_duration_seconds = self._read_wav_duration_seconds(audio_path)
        transcribe_kwargs = self._build_transcribe_kwargs(
            profile=profile, language=language, initial_prompt=initial_prompt
        )

        try:
            whisper_model = self._load_model(model_name=model, prefer_gpu=self.prefer_gpu)
            lines = self._run_transcription(
                whisper_model=whisper_model,
                audio_path=audio_path,
                transcribe_kwargs=transcribe_kwargs,
                audio_duration_seconds=audio_duration_seconds,
                progress_callback=progress_callback,
                timeout_sec=timeout_sec,
                cancel_event=cancel_event,
            )
        except TranscriptionTimeoutError:
            raise
        except Exception as exc:
            if self.prefer_gpu and _looks_like_cuda_failure(exc):
                # Transparent CPU fallback without another GPU attempt.
                whisper_model = self._load_model(model_name=model, prefer_gpu=False)
                try:
                    lines = self._run_transcription(
                        whisper_model=whisper_model,
                        audio_path=audio_path,
                        transcribe_kwargs=transcribe_kwargs,
                        audio_duration_seconds=audio_duration_seconds,
                        progress_callback=progress_callback,
                        timeout_sec=timeout_sec,
                        cancel_event=cancel_event,
                    )
                except TranscriptionTimeoutError:
                    raise
                except Exception as cpu_exc:
                    raise TranscriptionError(
                        f"Whisper failed during CPU fallback: {cpu_exc}"
                    ) from cpu_exc
            else:
                raise TranscriptionError(f"Whisper transcription failed: {exc}") from exc

        if not lines:
            raise TranscriptionError("Whisper returned empty transcription")

        return "\n".join(lines)

    # ---- Internal: run transcription with optional timeout ----------------------

    def _run_transcription(
        self,
        whisper_model,
        audio_path: Path,
        transcribe_kwargs: dict[str, object],
        audio_duration_seconds: float | None,
        progress_callback: Callable[[float], None] | None,
        timeout_sec: int,
        cancel_event: threading.Event | None,
    ) -> list[str]:
        # When a timeout is requested, push work into a worker thread so the
        # main thread can wake up after ``timeout_sec``. Otherwise, run
        # inline — but still honour ``cancel_event`` via the segment loop.
        if timeout_sec and timeout_sec > 0:
            return self._run_with_deadline(
                whisper_model=whisper_model,
                audio_path=audio_path,
                transcribe_kwargs=transcribe_kwargs,
                audio_duration_seconds=audio_duration_seconds,
                progress_callback=progress_callback,
                timeout_sec=timeout_sec,
                external_cancel=cancel_event,
            )
        segments, info = whisper_model.transcribe(str(audio_path), **transcribe_kwargs)
        self._record_detected_language(info, forced=bool(transcribe_kwargs.get("language")))
        return self._collect_lines_with_progress(
            segments=segments,
            audio_duration_seconds=audio_duration_seconds,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )

    def _run_with_deadline(
        self,
        whisper_model,
        audio_path: Path,
        transcribe_kwargs: dict[str, object],
        audio_duration_seconds: float | None,
        progress_callback: Callable[[float], None] | None,
        timeout_sec: int,
        external_cancel: threading.Event | None,
    ) -> list[str]:
        """Cooperative timeout: cancels on the next segment boundary.

        ``faster-whisper`` does not expose a hard cancellation primitive. We
        therefore set an event that is inspected by the segment iterator. The
        shortest reaction time is the duration of a single segment (a few
        seconds in practice).
        """
        cancel_event = threading.Event()
        result_queue: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)

        def _worker() -> None:
            try:
                segments, info = whisper_model.transcribe(
                    str(audio_path), **transcribe_kwargs
                )
                self._record_detected_language(
                    info, forced=bool(transcribe_kwargs.get("language"))
                )
                lines = self._collect_lines_with_progress(
                    segments=segments,
                    audio_duration_seconds=audio_duration_seconds,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                )
                result_queue.put(("ok", lines))
            except BaseException as exc:  # noqa: BLE001
                result_queue.put(("err", exc))

        worker = threading.Thread(target=_worker, name="whisper-transcribe", daemon=True)
        worker.start()
        # Poll either the worker queue or the external cancel signal.
        poll_interval = 0.5
        elapsed = 0.0
        while True:
            try:
                kind, payload = result_queue.get(timeout=poll_interval)
                break
            except queue.Empty:
                pass
            if external_cancel is not None and external_cancel.is_set():
                cancel_event.set()
                raise TranscriptionTimeoutError("Transcription cancelled") from None
            elapsed += poll_interval
            if elapsed >= timeout_sec:
                cancel_event.set()
                raise TranscriptionTimeoutError(
                    f"Transcription exceeded timeout ({timeout_sec}s)."
                ) from None

        if kind == "err":
            assert isinstance(payload, BaseException)
            raise payload
        assert isinstance(payload, list)
        return payload
