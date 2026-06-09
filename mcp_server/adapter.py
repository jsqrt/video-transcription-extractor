"""Pipeline adapter used by the MCP server.

Split out from ``server.py`` so the smoke tests can exercise the real
validation + response-shaping logic without pulling in the ``mcp`` SDK.

The response exposes:

* ``transcript_path``      — the ``<stem>.transcription.md`` file.
* ``summary_path``         — the ``<stem>.summary.md`` file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal, Optional

from app.models.types import (
    AppError,
    SummaryOptions,
    TranscribeOptions,
)
from app.services.audio_extractor import AudioExtractor
from app.services.cleanup import CleanMode
from app.services.pipeline import PipelineResult, run_pipeline
from app.services.scanner import DEFAULT_EXTENSIONS
from app.services.summarizer import Summarizer
from app.services.summary_writer import SummaryWriter
from app.services.transcriber import Transcriber
from app.services.writer import CleanTranscriptWriter


# The tool accepts every format the CLI can ingest (same Whisper pipeline),
# plus common audio containers. Keeping the list explicit prevents the MCP
# tool from being redirected to unexpected files on the user's machine.
_AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma",
}
_VIDEO_EXTENSIONS = DEFAULT_EXTENSIONS | {
    ".avi", ".webm", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg", ".ts", ".3gp",
}
ALLOWED_EXTENSIONS = frozenset(_AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS)

SummaryMode = Literal["ollama", "extractive", "none"]
_CLEAN_MODES: tuple[CleanMode, ...] = ("raw", "rule-based", "llm")


class AdapterError(Exception):
    """User-facing error. Message is safe to surface to the MCP client."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class TranscribeArguments:
    file_path: str
    output_dir: Optional[str] = None
    summary_mode: SummaryMode = "ollama"
    language: Optional[str] = None
    profile: str = "best"
    model: Optional[str] = None
    timeout_sec: int = 0
    clean_mode: CleanMode = "rule-based"
    write_clean: bool = True
    diarize: bool = False


@dataclass(frozen=True)
class TranscribeResponse:
    transcript_path: Optional[str]  # <stem>.transcription.md
    summary_path: Optional[str]
    duration_seconds: float
    utterance_count: int
    # Non-zero when Whisper produced a trailing repeat-loop artefact and
    # the Transcriber trimmed ``trail_loop_dropped`` consecutive duplicate
    # lines. Signals to the caller that audio content in the loop region
    # was likely lost and should be re-transcribed (e.g. with chunking).
    trail_loop_dropped: int = 0
    # Populated when summarization was requested but the LLM call failed.
    # The transcription file is still produced; this field tells the
    # caller WHY the summary is missing.
    summary_error: Optional[str] = None

    def as_dict(self) -> dict:
        data = asdict(self)
        # Keep paths as strings (callers expect JSON-friendly values).
        return data


PipelineCallable = Callable[..., PipelineResult]


class PipelineAdapter:
    """Validates inputs and delegates the actual work to the core pipeline.

    ``pipeline_fn`` and the service factories are injected so smoke tests can
    replace them with fakes. The defaults match what the CLI wires up in
    production.
    """

    def __init__(
        self,
        *,
        pipeline_fn: PipelineCallable = run_pipeline,
        extractor_factory: Optional[Callable[[], AudioExtractor]] = None,
        transcriber_factory: Optional[Callable[[], Transcriber]] = None,
        summarizer_factory: Optional[Callable[[SummaryOptions], Summarizer]] = None,
        clean_writer_factory: Optional[Callable[[CleanMode], CleanTranscriptWriter]] = None,
        summary_writer_factory: Optional[Callable[[], SummaryWriter]] = None,
        allowed_extensions: frozenset[str] = ALLOWED_EXTENSIONS,
        logger_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._pipeline_fn = pipeline_fn
        self._extractor_factory = extractor_factory or _default_extractor
        self._transcriber_factory = transcriber_factory or _default_transcriber
        self._summarizer_factory = summarizer_factory or _default_summarizer
        self._clean_writer_factory = clean_writer_factory or _default_clean_writer
        self._summary_writer_factory = summary_writer_factory or SummaryWriter
        self._allowed_extensions = allowed_extensions
        self._logger = logger_fn or (lambda _msg: None)

    # -- Validation -----------------------------------------------------------

    def _validate(self, args: TranscribeArguments) -> tuple[Path, Optional[Path]]:
        if not args.file_path or not isinstance(args.file_path, str):
            raise AdapterError("invalid_argument", "file_path must be a non-empty string")

        path = Path(args.file_path).expanduser()
        if not path.is_absolute():
            raise AdapterError(
                "invalid_argument",
                f"file_path must be absolute, got: {args.file_path!r}",
            )

        resolved = path.resolve(strict=False)
        if not resolved.exists():
            raise AdapterError("not_found", f"file_path does not exist: {resolved}")
        if not resolved.is_file():
            raise AdapterError("not_found", f"file_path is not a regular file: {resolved}")

        suffix = resolved.suffix.lower()
        if suffix not in self._allowed_extensions:
            allowed = ", ".join(sorted(self._allowed_extensions))
            raise AdapterError(
                "unsupported_format",
                f"Extension {suffix or '<none>'} is not supported. "
                f"Allowed: {allowed}",
            )

        output_dir: Optional[Path] = None
        if args.output_dir is not None:
            if not isinstance(args.output_dir, str) or not args.output_dir:
                raise AdapterError(
                    "invalid_argument",
                    "output_dir must be an absolute path or null",
                )
            out = Path(args.output_dir).expanduser()
            if not out.is_absolute():
                raise AdapterError(
                    "invalid_argument",
                    f"output_dir must be absolute, got: {args.output_dir!r}",
                )
            output_dir = out.resolve(strict=False)

        if args.summary_mode not in ("ollama", "extractive", "none"):
            raise AdapterError(
                "invalid_argument",
                f"summary_mode must be one of ollama|extractive|none, got: {args.summary_mode!r}",
            )
        # The extractive backend was removed when the summarizer became
        # LLM-only. Older MCP clients still pass ``extractive``; we treat
        # it as a synonym for ``ollama`` (with a logged warning) rather
        # than rejecting the request and breaking those callers.
        if args.summary_mode == "extractive":
            self._logger(
                "summary_mode='extractive' is deprecated — the extractive "
                "fallback was removed. Treating it as 'ollama'."
            )
        if args.profile not in ("fast", "best"):
            raise AdapterError(
                "invalid_argument",
                f"profile must be one of fast|best, got: {args.profile!r}",
            )
        if args.clean_mode not in _CLEAN_MODES:
            raise AdapterError(
                "invalid_argument",
                f"clean_mode must be one of raw|rule-based|llm, got: {args.clean_mode!r}",
            )
        if not isinstance(args.timeout_sec, int) or args.timeout_sec < 0:
            raise AdapterError(
                "invalid_argument",
                "timeout_sec must be a non-negative integer",
            )

        return resolved, output_dir

    # -- Entry point ----------------------------------------------------------

    def transcribe(
        self,
        args: TranscribeArguments,
        progress_fn: Optional[Callable[[float], None]] = None,
    ) -> TranscribeResponse:
        video_path, output_dir = self._validate(args)

        # Normalise the deprecated ``extractive`` alias before constructing
        # SummaryOptions so the downstream code only sees the currently
        # supported modes.
        normalised_mode = "ollama" if args.summary_mode == "extractive" else args.summary_mode
        summary_options = SummaryOptions(mode=normalised_mode)
        options = TranscribeOptions(
            model=args.model,
            profile=args.profile,
            language=args.language,
            timeout_sec=args.timeout_sec,
            diarize=bool(args.diarize),
            summary=summary_options,
        )

        try:
            result = self._pipeline_fn(
                video_path=video_path,
                options=options,
                output_dir=output_dir,
                clean_mode=args.clean_mode,
                write_clean_file=bool(args.write_clean),
                write_summary_file=(args.summary_mode != "none"),
                extractor=self._extractor_factory(),
                transcriber=self._transcriber_factory(),
                summarizer=self._summarizer_factory(summary_options),
                clean_writer=self._clean_writer_factory(args.clean_mode),
                summary_writer=self._summary_writer_factory(),
                progress_fn=progress_fn,
                logger_fn=self._logger,
            )
        except AdapterError:
            raise
        except AppError as exc:
            raise AdapterError("pipeline_error", str(exc)) from exc

        return TranscribeResponse(
            transcript_path=(
                str(result.transcript_path) if result.transcript_path else None
            ),
            summary_path=str(result.summary_path) if result.summary_path else None,
            duration_seconds=round(float(result.duration_seconds), 3),
            utterance_count=int(result.utterance_count),
            trail_loop_dropped=int(getattr(result, "trail_loop_dropped", 0) or 0),
            summary_error=getattr(result, "summary_error", None),
        )


# -- Default service factories (used in production) --------------------------


def _default_extractor() -> AudioExtractor:
    return AudioExtractor(sample_rate=16000)


def _default_transcriber() -> Transcriber:
    # Imported lazily so environments without faster-whisper installed can
    # still import ``mcp_server`` (e.g. for tests with fake pipelines).
    from app.providers.faster_whisper_provider import FasterWhisperProvider

    return Transcriber(provider=FasterWhisperProvider())


def _default_summarizer(options: SummaryOptions) -> Summarizer:
    llm_client = _build_ollama_client_if_reachable(options)
    return Summarizer(options=options, llm_client=llm_client)


def _default_clean_writer(clean_mode: CleanMode) -> CleanTranscriptWriter:
    llm_client = None
    if clean_mode == "llm":
        # Reuse the same defaults the summarizer uses so the MCP server
        # picks up a local Ollama seamlessly.
        llm_client = _build_ollama_client_if_reachable(SummaryOptions(mode="ollama"))
    return CleanTranscriptWriter(llm_client=llm_client)


def _build_ollama_client_if_reachable(options: SummaryOptions):
    # Deferred import — httpx is a runtime requirement but we don't want the
    # adapter import itself to fail in minimal test environments.
    from app.providers.ollama_provider import OllamaClient

    candidate = OllamaClient(
        base_url=options.ollama_base_url,
        model=options.ollama_model,
        timeout_sec=options.ollama_timeout_sec,
    )
    return candidate if candidate.is_available() else None
