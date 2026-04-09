from __future__ import annotations

import argparse
import tempfile
from typing import Callable
from pathlib import Path

from app.models.types import (
    AppError,
    AudioExtractionError,
    CliArgumentError,
    ModelNotFoundError,
    ProviderUnavailableError,
    ScanError,
    TranscriptionError,
    TranscriptionTimeoutError,
    TranscribeOptions,
)
from app.providers.faster_whisper_provider import FasterWhisperProvider
from app.providers.ollama_provider import OllamaProvider
from app.services.audio_extractor import AudioExtractor
from app.services.logger import CliLogger
from app.services.scanner import parse_extensions, scan_videos
from app.services.transcriber import Transcriber
from app.services.writer import TranscriptWriter, transcript_to_text

EXIT_SUCCESS = 0
EXIT_FAILED = 1
EXIT_BAD_ARGS = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="Local video-to-text transcription",
    )

    subparsers = parser.add_subparsers(dest="command")

    transcribe_parser = subparsers.add_parser("transcribe", help="Transcribe video")
    transcribe_parser.add_argument("--input", required=True, dest="input_path")
    transcribe_parser.add_argument("--output-dir", dest="output_dir")
    transcribe_parser.add_argument("--backend", default="faster-whisper")
    transcribe_parser.add_argument("--model", default=None)
    transcribe_parser.add_argument(
        "--profile",
        default="best",
        choices=["fast", "best"],
        help="Decoding profile: fast (faster) or best (higher quality).",
    )
    transcribe_parser.add_argument("--language", default=None)
    transcribe_parser.add_argument("--model-cache-dir", dest="model_cache_dir", default=None)
    transcribe_parser.add_argument(
        "--allow-online-model-download",
        action="store_true",
        dest="allow_online_model_download",
        help="Allow online Whisper model download (disabled by default).",
    )
    transcribe_parser.add_argument("--ext", default=None)
    transcribe_parser.add_argument("--stdout", action="store_true", dest="print_stdout")
    transcribe_parser.add_argument(
        "--progress",
        action="store_true",
        help="Show transcription progress percentage in terminal.",
    )
    transcribe_parser.add_argument("--verbose", action="store_true")
    transcribe_parser.add_argument("--timeout", type=int, default=300)

    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.command != "transcribe":
        raise CliArgumentError("Subcommand is required: transcribe")

    normalized_backend = args.backend.lower().replace("_", "-")
    if normalized_backend not in {"ollama", "faster-whisper"}:
        raise CliArgumentError(
            "Supported --backend values: ollama, faster-whisper"
        )

    if args.timeout <= 0:
        raise CliArgumentError("--timeout must be greater than 0")

    if args.allow_online_model_download:
        raise CliArgumentError(
            "Strict network isolation is enabled for this project. "
            "--allow-online-model-download is not permitted."
        )


def _resolve_model_name(options: TranscribeOptions) -> str:
    if options.model:
        return options.model
    return "small" if options.profile == "fast" else "large-v3"


def _process_single_video(
    video_path: Path,
    options: TranscribeOptions,
    output_dir: Path | None,
    print_stdout: bool,
    show_progress: bool,
    logger: CliLogger,
    extractor: AudioExtractor,
    transcriber: Transcriber,
    writer: TranscriptWriter,
) -> None:
    logger.status(video_path, "queued")

    with tempfile.TemporaryDirectory(prefix="vte_") as temp_dir:
        wav_path = Path(temp_dir) / f"{video_path.stem}.wav"

        logger.status(video_path, "extracting_audio")
        extractor.extract(video_path=video_path, output_wav_path=wav_path)

        logger.status(video_path, "transcribing")
        model_name = _resolve_model_name(options)

        progress_state = {"last": -1}

        def _on_progress(value: float) -> None:
            if not show_progress:
                return
            percent = max(0, min(100, int(value * 100)))
            if percent <= progress_state["last"]:
                return
            progress_state["last"] = percent
            print(f"[progress] {video_path.name}: {percent}%", end="\r", flush=True)

        progress_callback: Callable[[float], None] | None = _on_progress if show_progress else None
        transcript = transcriber.transcribe(
            audio_path=wav_path,
            model=model_name,
            profile=options.profile,
            language=options.language,
            timeout_sec=options.timeout_sec,
            progress_callback=progress_callback,
        )

        if show_progress and progress_state["last"] >= 0:
            print()

        output_path = writer.write(
            source_video=video_path,
            transcript=transcript,
            output_dir=output_dir,
        )

        if print_stdout:
            print(f"\n=== {video_path.name} ===")
            print(transcript_to_text(transcript))
            print()

        logger.info(f"Saved: {output_path}")
        logger.status(video_path, "done")


def run_transcribe(args: argparse.Namespace) -> int:
    _validate_args(args)

    logger = CliLogger(verbose=args.verbose)

    allowed_ext = parse_extensions(args.ext)
    videos = scan_videos(args.input_path, allowed_ext)

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
    options = TranscribeOptions(
        backend=args.backend.lower().replace("_", "-"),
        model=args.model,
        profile=args.profile,
        language=args.language,
        timeout_sec=args.timeout,
        model_cache_dir=args.model_cache_dir,
        allow_online_model_download=False,
    )

    if options.backend == "ollama":
        provider = OllamaProvider()
    else:
        provider = FasterWhisperProvider(
            allow_online_model_download=options.allow_online_model_download,
            model_cache_dir=Path(options.model_cache_dir).expanduser().resolve()
            if options.model_cache_dir
            else None,
        )
    extractor = AudioExtractor(sample_rate=16000)
    transcriber = Transcriber(provider=provider)
    writer = TranscriptWriter()

    failed = 0
    for video_path in videos:
        try:
            _process_single_video(
                video_path=video_path,
                options=options,
                output_dir=output_dir,
                print_stdout=args.print_stdout,
                show_progress=args.progress,
                logger=logger,
                extractor=extractor,
                transcriber=transcriber,
                writer=writer,
            )
        except (
            AudioExtractionError,
            ProviderUnavailableError,
            ModelNotFoundError,
            TranscriptionTimeoutError,
            TranscriptionError,
            AppError,
        ) as exc:
            failed += 1
            logger.status(video_path, "failed")
            logger.error(f"{video_path.name}: {exc}")

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


if __name__ == "__main__":
    raise SystemExit(main())
