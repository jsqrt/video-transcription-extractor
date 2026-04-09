from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Utterance:
    speaker: str
    text: str
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None


@dataclass(frozen=True)
class Transcript:
    utterances: list[Utterance]


@dataclass(frozen=True)
class TranscribeOptions:
    backend: str = "faster-whisper"
    model: Optional[str] = None
    profile: str = "best"
    language: Optional[str] = None
    timeout_sec: int = 300
    model_cache_dir: Optional[str] = None
    allow_online_model_download: bool = False


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


@dataclass(frozen=True)
class FileJob:
    source_video: Path
