"""Whisper transcription via the ``whisper.cpp`` runtime.

Shipped on macOS to give Apple Silicon GPU (Metal) acceleration that
CTranslate2 / faster-whisper cannot deliver. The provider speaks the
same protocol as :class:`FasterWhisperProvider` so the rest of the
pipeline does not care which backend produced the segments.

Backends compiled into ``pywhispercpp``'s prebuilt wheels:

* macOS arm64    → Metal (GPU)
* macOS x86_64   → Accelerate (CPU)
* Windows / Linux → CPU (no GPU support in the published wheels)

Hence the Windows shipping bundle still uses
:class:`FasterWhisperProvider` for NVIDIA CUDA. Override the runtime
choice via ``DESCRIBELY_ASR_BACKEND=whisper-cpp`` / ``=faster-whisper``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from app.models.types import (
    ModelNotFoundError,
    TranscriptionError,
    TranscriptionTimeoutError,
)


def pywhispercpp_available() -> bool:
    """Cheap probe: True iff ``pywhispercpp`` imports cleanly."""
    try:
        import pywhispercpp  # noqa: F401
    except Exception:
        return False
    return True


class WhisperCppProvider:
    """Drop-in replacement for FasterWhisperProvider on macOS.

    The model identifier passed to :meth:`transcribe` must be an
    absolute filesystem path to a GGML ``.bin`` file (the format
    whisper.cpp uses — different from the CTranslate2 directory layout
    faster-whisper expects). Use ``scripts/fetch_whisper_ggml.py`` to
    pre-seed one.
    """

    def __init__(self) -> None:
        self._model_cache: dict[str, object] = {}
        self._load_lock = threading.Lock()

    # ---- Model loading ----------------------------------------------------------

    def _load_model(self, model_path: str):
        if model_path in self._model_cache:
            return self._model_cache[model_path]

        with self._load_lock:
            if model_path in self._model_cache:
                return self._model_cache[model_path]
            try:
                from pywhispercpp.model import Model  # noqa: WPS433
            except ImportError as exc:
                raise TranscriptionError(
                    "pywhispercpp is not installed. The macOS ASR path "
                    "cannot run without it."
                ) from exc

            if not Path(model_path).is_file():
                raise ModelNotFoundError(
                    f"Whisper GGML model file not found: {model_path}. "
                    "Run scripts/fetch_whisper_ggml.py before launching."
                )

            try:
                model = Model(model=model_path)
            except Exception as exc:  # noqa: BLE001
                raise TranscriptionError(
                    f"Failed to load whisper.cpp model: {exc}"
                ) from exc

            self._model_cache[model_path] = model
            return model

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
    ) -> str:
        """Produce the same line-oriented output that the Transcriber
        wrapper parses: ``[start -> end] [SPEAKER] text``.

        ``timeout_sec`` is honoured cooperatively via ``cancel_event``;
        whisper.cpp does not expose a hard cancellation primitive, so
        cancellation only fires between segments (same trade-off as
        faster-whisper).
        """
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionTimeoutError("Transcription cancelled")

        whisper_model = self._load_model(model)

        # Map the project's profile names onto whisper.cpp's controls.
        # ``best_of`` / ``beam_size`` work like in faster-whisper.
        normalized_profile = (profile or "best").lower()
        if normalized_profile == "fast":
            params: dict[str, object] = {"beam_size": 2, "best_of": 2}
        else:
            params = {"beam_size": 5, "best_of": 5}
        if language:
            params["language"] = language
        else:
            # whisper.cpp uses "auto" for auto-detection.
            params["language"] = "auto"

        try:
            segments = whisper_model.transcribe(str(audio_path), **params)
        except Exception as exc:  # noqa: BLE001
            raise TranscriptionError(f"Whisper transcription failed: {exc}") from exc

        # whisper.cpp returns segments as objects with t0 / t1 (in
        # centiseconds) and text. We translate to the same format the
        # Transcriber parser expects from FasterWhisperProvider so the
        # rest of the pipeline is identical.
        lines: list[str] = []
        for segment in segments:
            if cancel_event is not None and cancel_event.is_set():
                raise TranscriptionTimeoutError("Transcription cancelled")

            text = (getattr(segment, "text", "") or "").strip()
            if not text:
                continue
            # pywhispercpp exposes timestamps in centiseconds (×10ms).
            start_cs = float(getattr(segment, "t0", 0.0) or 0.0)
            end_cs = float(getattr(segment, "t1", start_cs) or start_cs)
            start_sec = start_cs / 100.0
            end_sec = end_cs / 100.0
            if end_sec < start_sec:
                start_sec, end_sec = end_sec, start_sec
            lines.append(
                f"[{start_sec:.2f} -> {end_sec:.2f}] [UNKNOWN_SPEAKER] {text}"
            )

        if progress_callback:
            progress_callback(1.0)

        if not lines:
            raise TranscriptionError("Whisper returned empty transcription")

        return "\n".join(lines)

    def release(self) -> None:
        """Drop cached models so Metal/CUDA memory comes back to Ollama."""
        self._model_cache.clear()
        import gc
        gc.collect()


__all__ = ["WhisperCppProvider", "pywhispercpp_available"]
