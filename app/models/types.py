from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Utterance:
    speaker: str
    text: str
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None


@dataclass(frozen=True)
class Transcript:
    utterances: tuple[Utterance, ...] = field(default_factory=tuple)
    # ISO-639-1 code Whisper detected for the audio (e.g. "uk", "ru",
    # "en"). ``None`` when the provider couldn't detect or the user
    # forced a language. Used by the summarizer to instruct the LLM in
    # an exact language — the LLM alone can't reliably distinguish
    # Ukrainian from Russian on Cyrillic text, but Whisper can.
    detected_language: Optional[str] = None
    # Confidence in ``detected_language`` (0..1), best-effort.
    detected_language_probability: Optional[float] = None


SummaryMode = str  # one of: "none", "extractive", "ollama"


@dataclass(frozen=True)
class SummaryOptions:
    mode: SummaryMode = "ollama"
    ollama_base_url: str = "http://127.0.0.1:11434"
    # The summary model is FIXED to qwen2.5:7b. We run the standalone-chunk
    # → concatenate pipeline on it with small (~450-token) chunks. 7b is the
    # quality bar: the 3b model hallucinates on spontaneous Ukrainian
    # (invents details, breaks role, garbles morphology), which 7b does not.
    # There is deliberately NO fallback to "whatever else is installed" and
    # no silent downgrade to 3b: if this exact model is missing we surface
    # that and skip the summary rather than degrade.
    ollama_model: str = "qwen2.5:7b"
    # 10 minutes per Ollama call. The map-reduce pipeline makes one
    # extraction call per chunk (~30-60 s on a 7B model on M-series) plus
    # a final reduce call that has to merge 8-12 drafts (~3-5 min). The
    # previous 180 s default was tight enough that the reduce step
    # silently timed out on a 22-minute Ukrainian news transcript on
    # qwen2.5:7b, leaving the user with a transcript but no summary.
    # This is a per-call cap, not a total — healthy calls finish well
    # under it.
    ollama_timeout_sec: int = 600


@dataclass(frozen=True)
class TranscribeOptions:
    model: Optional[str] = None
    profile: str = "best"
    language: Optional[str] = None
    timeout_sec: int = 0  # 0 = disabled
    model_cache_dir: Optional[str] = None
    diarize: bool = False
    summary: SummaryOptions = field(default_factory=SummaryOptions)
    # Comma-separated vocabulary hint passed to Whisper. Biases the
    # decoder toward specific terms (names, English loanwords, jargon)
    # that the model would otherwise mis-transcribe or "translate" into
    # a phonetically-similar word. Capped by Whisper at ~224 tokens.
    initial_prompt: Optional[str] = None


class AppError(Exception):
    """Base app exception with user-friendly message."""


class CliArgumentError(AppError):
    pass


class ScanError(AppError):
    pass


class AudioExtractionError(AppError):
    pass


class ProviderUnavailableError(AppError):
    pass


class ModelNotFoundError(AppError):
    pass


class TranscriptionTimeoutError(AppError):
    pass


class TranscriptionError(AppError):
    pass


class SummarizationError(AppError):
    pass


class SummarizationTimeoutError(AppError):
    pass
