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


SummaryMode = str  # one of: "none", "extractive", "ollama"


@dataclass(frozen=True)
class SummaryOptions:
    mode: SummaryMode = "ollama"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_timeout_sec: int = 180
    per_chapter_sentences: int = 3
    overview_sentences: int = 5
    title_max_words: int = 7


@dataclass(frozen=True)
class TranscribeOptions:
    model: Optional[str] = None
    profile: str = "best"
    language: Optional[str] = None
    timeout_sec: int = 0  # 0 = disabled
    model_cache_dir: Optional[str] = None
    include_chapters: bool = True
    diarize: bool = False
    summary: SummaryOptions = field(default_factory=SummaryOptions)


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
