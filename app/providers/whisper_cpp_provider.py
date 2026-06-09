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

import os
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
        # Populated by the last call to ``transcribe`` so the pipeline
        # can route the detected language into the summarizer prompt.
        # ``None`` means the user forced a language or detection failed.
        self.last_detected_language: str | None = None
        self.last_detected_language_probability: float | None = None

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

    # ---- Helpers ----------------------------------------------------------------

    @staticmethod
    def _auto_detect_language(
        whisper_model, audio_path: Path, n_threads: int | None
    ) -> tuple[str, float] | None:
        """Detect spoken language on the first 30 s window.

        Whisper's acoustic features distinguish Ukrainian and Russian
        far more reliably than any downstream text-only LLM can on
        Cyrillic — so we capture the answer here and pass it on. The
        call is cheap (a single 30 s mel + one decoder pass) and runs
        before the full transcribe, so a failure here must NEVER abort
        the pipeline. Returns ``None`` on any failure; the caller then
        leaves the decoder in ``language="auto"``.
        """
        try:
            detect = getattr(whisper_model, "auto_detect_language", None)
            if detect is None:
                return None
            threads = int(n_threads) if n_threads else 4
            result = detect(str(audio_path), n_threads=threads)
        except Exception:  # noqa: BLE001
            return None

        # pywhispercpp returns ((code, probability), {code: prob, ...}).
        try:
            (code, probability), _all = result
        except (TypeError, ValueError):
            return None
        code = str(code or "").strip().lower()
        if not code:
            return None
        try:
            probability_f = float(probability)
        except (TypeError, ValueError):
            probability_f = 0.0
        return code, probability_f

    @staticmethod
    def _audio_duration_sec(audio_path: Path) -> float:
        """Duration of the (16 kHz mono) wav, for progress estimation.

        The pipeline always feeds us a wav, so we read the header rather
        than decoding the whole file. Returns 0.0 if it can't be read —
        the caller then simply skips live progress updates.
        """
        try:
            import wave

            with wave.open(str(audio_path), "rb") as wav:
                frames = wav.getnframes()
                rate = wav.getframerate() or 16000
                return frames / float(rate)
        except Exception:  # noqa: BLE001
            return 0.0

    # Decoder configs that pywhispercpp consumes itself (not attributes of
    # the underlying whisper_full_params object), so they must never be
    # filtered out by the attribute check.
    _NESTED_PARAM_KEYS = frozenset({"beam_search", "greedy"})

    @classmethod
    def _drop_unsupported_params(
        cls, whisper_model, params: dict[str, object]
    ) -> dict[str, object]:
        """Return ``params`` minus any flat key the model's params object
        doesn't expose, so a pywhispercpp/whisper.cpp version skew can't
        crash transcription with an ``AttributeError`` on setattr."""
        target = getattr(whisper_model, "_params", None)
        if target is None:
            return params
        kept: dict[str, object] = {}
        for key, value in params.items():
            if key in cls._NESTED_PARAM_KEYS or hasattr(target, key):
                kept[key] = value
        return kept

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
        """Produce the same line-oriented output that the Transcriber
        wrapper parses: ``[start -> end] [SPEAKER] text``.

        ``timeout_sec`` is honoured cooperatively via ``cancel_event``;
        whisper.cpp does not expose a hard cancellation primitive, so
        cancellation only fires between segments (same trade-off as
        faster-whisper).

        ``initial_prompt`` is a comma-separated list of domain terms /
        names / English borrowings that biases the decoder toward
        recognising them as whole tokens. Whisper's prompt budget is
        ~224 tokens (~150 words); longer prompts are truncated upstream
        by whisper.cpp. Critical for Ukrainian where the default model
        otherwise "translates" technical English loanwords into
        Ukrainian or fuses them with adjacent numerals
        (e.g. "пейджа два" → "пейдждва").
        """
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionTimeoutError("Transcription cancelled")

        # Reset detection state — each call must reflect THIS file only.
        self.last_detected_language = None
        self.last_detected_language_probability = None

        whisper_model = self._load_model(model)

        # Map the project's profile names onto whisper.cpp's controls.
        # pywhispercpp uses nested dicts for beam_search / greedy params,
        # not flat beam_size / best_of like faster-whisper.
        #
        # ``best`` uses beam search for accuracy; ``fast`` uses a narrower
        # beam for speed. Beam width matters a lot for under-resourced
        # languages like Ukrainian, where greedy on a small model produces
        # noticeably more garbage. beam=5 is the OpenAI reference setting
        # and gives the best WER on Ukrainian morphology (catches subtle
        # vowel distinctions like "додавання" vs "додування" that beam=3
        # sometimes misses on a quantized medium model).
        normalized_profile = (profile or "best").lower()
        if normalized_profile == "fast":
            params: dict[str, object] = {
                "beam_search": {"beam_size": 2, "patience": -1.0},
                "greedy": {"best_of": 2},
            }
        else:
            params = {
                "beam_search": {"beam_size": 5, "patience": -1.0},
                "greedy": {"best_of": 5},
            }

        # Anti-hallucination / anti-repeat-loop controls. These are the
        # whisper.cpp equivalents of faster-whisper's quality knobs and are
        # what stop the model from (a) inventing lyrics over music/intros
        # and (b) entering "Добрий день, Добрий день…" repeat-loops:
        #
        #  - no_context: don't feed the previous segment back in as the
        #    prompt. Carried context is the single biggest driver of the
        #    runaway-repeat failure mode.
        #  - temperature + temperature_inc: enable the fallback ladder.
        #    When a segment decodes with low confidence (high entropy or
        #    low avg-logprob, or compresses too well = repetition),
        #    whisper.cpp re-decodes it at a higher temperature. THIS is the
        #    mechanism that breaks repeat-loops — disabling it (inc=0) was
        #    what let the loops through.
        #  - entropy_thold / logprob_thold: thresholds that trigger that
        #    fallback. The 2.4 entropy threshold is whisper's default for
        #    detecting "this segment is suspiciously repetitive".
        #  - no_speech_thold: treat a segment as silence (emit nothing)
        #    when the no-speech probability is high — kills invented text
        #    over music and pauses.
        #  - suppress_blank: don't let the decoder spend probability mass
        #    on blank tokens.
        #  - audio_ctx: how many encoder context frames to attend to. The
        #    full mel context is 1500. We use the full context by default
        #    because the encoder accuracy loss from capping is measurable
        #    on Ukrainian (homophone confusions like "додавання" vs
        #    "додування" benefit from the full 30 s look-ahead). Override
        #    via DESCRIBELY_WHISPER_AUDIO_CTX to cap (e.g. 768 for ~1.5×
        #    speedup at the cost of WER on morphology-rich languages).
        #
        # NOTE: the set of accepted fields differs between whisper.cpp /
        # pywhispercpp builds (e.g. ``suppress_non_speech_tokens`` was
        # renamed to ``suppress_nst`` upstream and is absent from some
        # wheels). We therefore filter against the real ``Params`` object
        # below rather than trusting pywhispercpp's PARAMS_SCHEMA, which
        # lists fields the compiled object may not actually have.
        params.update(
            {
                "no_context": True,
                "temperature": 0.0,
                "temperature_inc": 0.2,
                "entropy_thold": 2.4,
                "logprob_thold": -1.0,
                "no_speech_thold": 0.6,
                "suppress_blank": True,
            }
        )
        audio_ctx_env = os.environ.get("DESCRIBELY_WHISPER_AUDIO_CTX")
        if audio_ctx_env is not None:
            audio_ctx = int(audio_ctx_env)
            if audio_ctx > 0:
                params["audio_ctx"] = audio_ctx

        # Initial prompt: biases the decoder toward specific vocabulary
        # (domain terms, English loanwords, names) so it picks the right
        # spelling instead of substituting a phonetically-similar
        # Ukrainian word. whisper.cpp ignores anything beyond ~224
        # tokens, so even an over-long prompt is safe — it's just
        # truncated. An empty/whitespace-only prompt is skipped so we
        # don't override whisper.cpp's default behaviour.
        prompt_text = (initial_prompt or "").strip()
        if prompt_text:
            params["initial_prompt"] = prompt_text

        # Use all performance cores minus 2 (reserved for UI + system).
        # Default is 4; on M4 (10 cores) this gives 8 threads → measurably
        # faster. Allow override via env var for user tuning. ``os.cpu_count()``
        # may return None on exotic hosts, so fall back to a safe default
        # before the arithmetic rather than crashing with a TypeError.
        cpu_count = os.cpu_count() or 6
        n_threads = int(
            os.environ.get("DESCRIBELY_WHISPER_THREADS")
            or max(4, cpu_count - 2)
        )
        params["n_threads"] = n_threads

        if language:
            # The user explicitly forced a language — they've decided
            # they want monolingual decoding and accept that English
            # loanwords will be re-written in the forced script. Trust
            # them and skip detection.
            params["language"] = language
        else:
            # Mixed-language mode (default): we DO NOT pin the decoder
            # to any single language. Pinning to "uk" makes Whisper
            # "translate" English loanwords ("пейдж", "фронтенд",
            # "деплой") into phonetically-similar Ukrainian words. By
            # passing "auto" (per-segment detection) Whisper picks the
            # language for each chunk individually, so an English term
            # inside Ukrainian speech can stay in English script.
            #
            # We still run the 30 s auto-detect to populate
            # last_detected_language for the summarizer — a text-only
            # LLM is bad at distinguishing Ukrainian from Russian on
            # Cyrillic, and Whisper's acoustic answer is much more
            # reliable than anything we could recover from the
            # transcript text alone.
            detected = self._auto_detect_language(
                whisper_model, audio_path, n_threads
            )
            if detected is not None:
                code, probability = detected
                self.last_detected_language = code
                self.last_detected_language_probability = probability
            params["language"] = "auto"

        # Drop any flat param this pywhispercpp build's whisper_full_params
        # object doesn't accept, so a version skew degrades gracefully
        # (param simply ignored) instead of raising AttributeError mid-run.
        # ``beam_search`` / ``greedy`` are nested decoder configs handled
        # specially by pywhispercpp, not attributes of the params object,
        # so they're always kept.
        params = self._drop_unsupported_params(whisper_model, params)

        # Drive a live progress bar: whisper.cpp emits segments roughly in
        # chronological order, so the end timestamp of the latest segment
        # over the total audio duration is a good completion estimate.
        # (The native progress_callback param is not assignable through
        # pywhispercpp's pybind layer, but new_segment_callback is.)
        total_sec = self._audio_duration_sec(audio_path)

        def _on_segment(segment) -> None:
            if cancel_event is not None and cancel_event.is_set():
                # whisper.cpp has no hard cancel; raising here aborts the
                # transcribe() call between segments.
                raise TranscriptionTimeoutError("Transcription cancelled")
            if progress_callback and total_sec > 0:
                end_sec = float(getattr(segment, "t1", 0.0) or 0.0) / 100.0
                progress_callback(max(0.0, min(0.99, end_sec / total_sec)))

        try:
            segments = whisper_model.transcribe(
                str(audio_path),
                new_segment_callback=_on_segment,
                **params,
            )
        except TranscriptionTimeoutError:
            raise
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
