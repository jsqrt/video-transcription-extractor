from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

from app.models.types import (
    AppError,
    AudioExtractionError,
    CliArgumentError,
    ModelNotFoundError,
    ProviderUnavailableError,
    ScanError,
    SummarizationError,
    SummarizationTimeoutError,
    SummaryOptions,
    TranscribeOptions,
    TranscriptionError,
    TranscriptionTimeoutError,
)
from app.providers.faster_whisper_provider import FasterWhisperProvider
from app.services.audio_extractor import AudioExtractor
from app.services.cleanup import CleanMode
from app.services.logger import CliLogger
from app.services.pipeline import run_pipeline
from app.services.raw_writer import RawTranscriptWriter
from app.services.scanner import parse_extensions, scan_videos
from app.services.summarizer import Summarizer
from app.services.summary_writer import SummaryWriter
from app.services.transcriber import Transcriber
from app.services.writer import CleanTranscriptWriter

EXIT_SUCCESS = 0
EXIT_FAILED = 1
EXIT_BAD_ARGS = 2

_SUMMARY_CHOICES = ("none", "extractive", "ollama")
_TITLE_STYLE_CHOICES = ("keywords", "snippet")
_CLEAN_MODE_CHOICES = ("raw", "rule-based", "llm")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="Local video-to-text transcription with optional GPU-accelerated summarization.",
    )

    subparsers = parser.add_subparsers(dest="command")

    transcribe_parser = subparsers.add_parser("transcribe", help="Transcribe video")
    transcribe_parser.add_argument("--input", required=True, dest="input_path")
    transcribe_parser.add_argument("--output-dir", dest="output_dir")
    transcribe_parser.add_argument(
        "--model",
        default=None,
        help="Whisper model name (tiny/base/small/medium/large-v3). "
             "Default depends on --profile.",
    )
    transcribe_parser.add_argument(
        "--profile",
        default="best",
        choices=["fast", "best"],
        help="Decoding profile: fast (faster) or best (higher quality).",
    )
    transcribe_parser.add_argument("--language", default=None)
    transcribe_parser.add_argument(
        "--model-cache-dir", dest="model_cache_dir", default=None,
        help="Local directory that holds the Whisper model cache.",
    )
    transcribe_parser.add_argument(
        "--ext",
        default=None,
        help="Comma-separated list of extensions when --input is a directory. "
             "Default: mp4,mov,mkv",
    )
    transcribe_parser.add_argument("--stdout", action="store_true", dest="print_stdout")
    transcribe_parser.add_argument(
        "--progress",
        action="store_true",
        help="Show transcription progress percentage in terminal.",
    )
    transcribe_parser.add_argument("--verbose", action="store_true")
    transcribe_parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="Per-file transcription timeout in seconds (0 = no timeout).",
    )
    transcribe_parser.add_argument(
        "--no-chapters",
        action="store_false",
        dest="include_chapters",
        help="Disable thematic chaptering in output transcript.",
    )
    transcribe_parser.set_defaults(include_chapters=True)

    transcribe_parser.add_argument(
        "--title-style",
        default="keywords",
        choices=_TITLE_STYLE_CHOICES,
        help="Chapter title style used BEFORE LLM refinement (or when "
             "--summary=none). 'keywords' = top terms; 'snippet' = first "
             "meaningful phrase from the chapter.",
    )

    # ---- Summarization --------------------------------------------------------
    transcribe_parser.add_argument(
        "--summary",
        default="ollama",
        choices=_SUMMARY_CHOICES,
        help="Summarization backend. 'none' disables the summary file and "
             "leaves chapter titles as the initial heuristic. 'extractive' "
             "runs a fast offline summarizer. 'ollama' (default) uses a "
             "local Ollama LLM with JSON structured output and gracefully "
             "falls back to extractive if Ollama is unreachable.",
    )
    transcribe_parser.add_argument(
        "--no-summary-file",
        action="store_false",
        dest="summary_file",
        help="Do not write the separate <name>.summary.md file.",
    )
    transcribe_parser.set_defaults(summary_file=True)

    # ---- Output files ---------------------------------------------------------
    transcribe_parser.add_argument(
        "--no-raw-file",
        action="store_false",
        dest="raw_file",
        help="Do not write the verbatim <name>.raw.txt file.",
    )
    transcribe_parser.set_defaults(raw_file=True)

    transcribe_parser.add_argument(
        "--no-clean-file",
        action="store_false",
        dest="clean_file",
        help="Do not write the human-readable <name>.clean.md file.",
    )
    transcribe_parser.set_defaults(clean_file=True)

    transcribe_parser.add_argument(
        "--clean-mode",
        default="rule-based",
        choices=_CLEAN_MODE_CHOICES,
        help="Cleanup mode for <name>.clean.md. 'raw' = no cleanup (just "
             "chapter formatting); 'rule-based' (default) = exact dedup + "
             "rolling-overlap dedup + sentence stitching + filler collapse; "
             "'llm' = rule-based pass + conservative LLM polish, validated "
             "by word-subsequence check (fails-safe to rule-based).",
    )

    transcribe_parser.add_argument(
        "--ollama-model",
        default="llama3.1:8b",
        help="Ollama model used when --summary=ollama. Good picks for "
             "Ukrainian/multilingual: llama3.1:8b, qwen2.5:7b, gemma2:9b.",
    )
    transcribe_parser.add_argument(
        "--ollama-url",
        default="http://127.0.0.1:11434",
        help="Ollama base URL (must resolve to a loopback address in offline mode).",
    )
    transcribe_parser.add_argument(
        "--ollama-timeout",
        type=int,
        default=180,
        help="Per-request timeout for Ollama summarization (seconds).",
    )
    transcribe_parser.add_argument(
        "--summary-per-chapter",
        type=int,
        default=3,
        help="Target number of sentences per chapter in the summary file.",
    )
    transcribe_parser.add_argument(
        "--summary-overview",
        type=int,
        default=5,
        help="Target number of sentences in the top-level overview.",
    )
    transcribe_parser.add_argument(
        "--title-max-words",
        type=int,
        default=7,
        help="Maximum words in refined chapter titles.",
    )

    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.command != "transcribe":
        raise CliArgumentError("Subcommand is required: transcribe")

    if args.timeout < 0:
        raise CliArgumentError("--timeout must be >= 0")

    if args.summary_per_chapter <= 0:
        raise CliArgumentError("--summary-per-chapter must be > 0")
    if args.summary_overview <= 0:
        raise CliArgumentError("--summary-overview must be > 0")
    if args.title_max_words <= 0:
        raise CliArgumentError("--title-max-words must be > 0")


def _resolve_model_name(options: TranscribeOptions) -> str:
    if options.model:
        return options.model
    return "small" if options.profile == "fast" else "large-v3"


def _make_summary_options(args: argparse.Namespace) -> SummaryOptions:
    return SummaryOptions(
        mode=args.summary,
        ollama_base_url=args.ollama_url,
        ollama_model=args.ollama_model,
        ollama_timeout_sec=args.ollama_timeout,
        per_chapter_sentences=args.summary_per_chapter,
        overview_sentences=args.summary_overview,
        title_max_words=args.title_max_words,
    )


def _resolve_cleanup_client(
    *, clean_mode: str, summary_options: SummaryOptions, logger: CliLogger
):
    """Return an Ollama client for LLM cleanup, or None.

    We deliberately reuse the same Ollama URL/model/timeout the user
    configured for summarization so ``--clean-mode=llm`` works out of the
    box once Ollama is up. If Ollama isn't reachable we log once and let
    the cleanup module itself fall back to rule-based per call.
    """
    if clean_mode != "llm":
        return None
    # Lazy import so `python -m app transcribe --help` and the unit tests
    # don't pull in httpx (the Ollama client's HTTP dep) transitively.
    from app.providers.ollama_provider import OllamaClient

    candidate = OllamaClient(
        base_url=summary_options.ollama_base_url,
        model=summary_options.ollama_model,
        timeout_sec=summary_options.ollama_timeout_sec,
    )
    if candidate.is_available():
        logger.info(
            "Ollama ready for LLM cleanup: "
            f"model={summary_options.ollama_model} at {summary_options.ollama_base_url}"
        )
        return candidate
    logger.info(
        "Ollama not reachable; --clean-mode=llm will fall back to rule-based."
    )
    return None


def _build_summarizer(options: SummaryOptions, logger: CliLogger) -> Summarizer:
    llm_client = None
    if options.mode == "ollama":
        # Lazy import so tests and CLI-only paths don't need httpx.
        from app.providers.ollama_provider import OllamaClient

        candidate = OllamaClient(
            base_url=options.ollama_base_url,
            model=options.ollama_model,
            timeout_sec=options.ollama_timeout_sec,
        )
        if candidate.is_available():
            llm_client = candidate
            logger.info(
                f"Ollama ready: model={options.ollama_model} at {options.ollama_base_url}"
            )
        else:
            logger.info(
                "Ollama is not reachable on the configured URL; falling back to extractive."
            )
    return Summarizer(options=options, llm_client=llm_client, logger_fn=logger.info)


def _process_single_video(
    video_path: Path,
    options: TranscribeOptions,
    output_dir: Path | None,
    print_stdout: bool,
    show_progress: bool,
    title_style: str,
    clean_mode: CleanMode,
    write_raw_file: bool,
    write_clean_file: bool,
    write_summary_file: bool,
    logger: CliLogger,
    extractor: AudioExtractor,
    transcriber: Transcriber,
    summarizer: Summarizer,
    clean_writer: CleanTranscriptWriter,
    raw_writer: RawTranscriptWriter,
    summary_writer: SummaryWriter,
) -> None:
    logger.status(video_path, "queued")
    logger.status(video_path, "processing")

    progress_state = {"last": -1}

    def _on_progress(value: float) -> None:
        if not show_progress:
            return
        percent = max(0, min(100, int(value * 100)))
        if percent <= progress_state["last"]:
            return
        progress_state["last"] = percent
        print(f"[progress] {video_path.name}: {percent}%", end="\r", flush=True)

    progress_callback: Callable[[float], None] | None = (
        _on_progress if show_progress else None
    )

    result = run_pipeline(
        video_path=video_path,
        options=options,
        output_dir=output_dir,
        title_style=title_style,
        clean_mode=clean_mode,
        write_raw_file=write_raw_file,
        write_clean_file=write_clean_file,
        write_summary_file=write_summary_file,
        extractor=extractor,
        transcriber=transcriber,
        summarizer=summarizer,
        clean_writer=clean_writer,
        raw_writer=raw_writer,
        summary_writer=summary_writer,
        progress_fn=progress_callback,
        logger_fn=logger.info,
        model_name=_resolve_model_name(options),
    )

    if show_progress and progress_state["last"] >= 0:
        print()

    if result.raw_transcript_path is not None:
        logger.info(f"Saved raw transcript: {result.raw_transcript_path}")
    if result.transcript_path is not None:
        logger.info(f"Saved transcript: {result.transcript_path}")
    if result.summary_path is not None:
        logger.info(f"Saved summary: {result.summary_path}")

    if print_stdout and result.transcript_path is not None:
        print(f"\n=== {video_path.name} ===")
        # Read the file we just wrote so stdout mirrors the saved artefact
        # exactly (includes chapter headings / refined titles).
        print(result.transcript_path.read_text(encoding="utf-8"))
        print()

    logger.status(video_path, "done")


def run_transcribe(args: argparse.Namespace) -> int:
    _validate_args(args)

    logger = CliLogger(verbose=args.verbose)

    allowed_ext = parse_extensions(args.ext)
    videos = scan_videos(args.input_path, allowed_ext)

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None

    summary_options = _make_summary_options(args)
    options = TranscribeOptions(
        model=args.model,
        profile=args.profile,
        language=args.language,
        timeout_sec=args.timeout,
        model_cache_dir=args.model_cache_dir,
        include_chapters=args.include_chapters,
        summary=summary_options,
    )

    provider = FasterWhisperProvider(
        model_cache_dir=Path(options.model_cache_dir).expanduser().resolve()
        if options.model_cache_dir
        else None,
    )
    extractor = AudioExtractor(sample_rate=16000)
    transcriber = Transcriber(provider=provider, logger_fn=logger.warn)
    summarizer = _build_summarizer(options=summary_options, logger=logger)
    llm_cleanup_client = _resolve_cleanup_client(
        clean_mode=args.clean_mode,
        summary_options=summary_options,
        logger=logger,
    )
    clean_writer = CleanTranscriptWriter(
        llm_client=llm_cleanup_client,
        logger_fn=logger.info,
    )
    raw_writer = RawTranscriptWriter()
    summary_writer = SummaryWriter()

    failed = 0
    for video_path in videos:
        try:
            _process_single_video(
                video_path=video_path,
                options=options,
                output_dir=output_dir,
                print_stdout=args.print_stdout,
                show_progress=args.progress,
                title_style=args.title_style,
                clean_mode=args.clean_mode,
                write_raw_file=args.raw_file,
                write_clean_file=args.clean_file,
                write_summary_file=args.summary_file,
                logger=logger,
                extractor=extractor,
                transcriber=transcriber,
                summarizer=summarizer,
                clean_writer=clean_writer,
                raw_writer=raw_writer,
                summary_writer=summary_writer,
            )
        except (
            AudioExtractionError,
            ProviderUnavailableError,
            ModelNotFoundError,
            TranscriptionTimeoutError,
            TranscriptionError,
            SummarizationError,
            SummarizationTimeoutError,
            AppError,
        ) as exc:
            failed += 1
            logger.status(video_path, "failed")
            logger.error(f"{video_path.name}: {exc}")
        except KeyboardInterrupt:
            logger.status(video_path, "cancelled")
            return EXIT_FAILED

    return EXIT_SUCCESS if failed == 0 else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return run_transcribe(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_BAD_ARGS
        return code
    except (CliArgumentError, ScanError) as exc:
        print(f"[error] {exc}")
        return EXIT_BAD_ARGS
    except KeyboardInterrupt:
        print("\n[cancelled]")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
