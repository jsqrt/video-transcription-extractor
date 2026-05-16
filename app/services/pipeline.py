"""High-level pipeline wrapper shared by the CLI and the MCP server.

The CLI and the MCP server both need to perform exactly the same sequence of
steps for a single media file:

    extract audio → transcribe → cleanup → chapterize → summarize → write files.

Three artefacts are produced per video:

* ``<stem>.raw.txt``     — verbatim Whisper output (if enabled).
* ``<stem>.clean.md``    — readable markdown after cleanup (if enabled).
* ``<stem>.summary.md``  — four-section summary (if enabled and summary is on).

The cleaned transcript (same one that lands in ``.clean.md``) is what the
summarizer sees, so chapters and summary match the file the user reads.

This function is deliberately dependency-injection friendly: every service
has a default but can be overridden from tests.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.models.types import Transcript, TranscribeOptions
from app.services.audio_extractor import AudioExtractor
from app.services.chapterizer import ChapterTitleStyle
from app.services.cleanup import CleanMode
from app.services.raw_writer import RawTranscriptWriter
from app.services.summarizer import Summarizer, SummaryResult
from app.services.summary_writer import SummaryWriter
from app.services.transcriber import Transcriber
from app.services.writer import (
    CleanTranscriptWriter,
    build_chapters_for_transcript,
    refined_titles_from_summary,
)


@dataclass(frozen=True)
class PipelineResult:
    """Everything a caller (CLI / MCP / tests) typically wants to report."""

    transcript_path: Optional[Path]  # <stem>.clean.md
    raw_transcript_path: Optional[Path]  # <stem>.raw.txt
    summary_path: Optional[Path]  # <stem>.summary.md
    duration_seconds: float
    chapter_count: int
    utterance_count: int
    # Number of trailing lines trimmed because Whisper entered a
    # repeat-loop at end-of-audio. Non-zero means audio content was
    # likely lost in that region. Zero on healthy transcripts.
    trail_loop_dropped: int = 0


ProgressFn = Callable[[float], None]
LoggerFn = Callable[[str], None]


def _duration_from_transcript(transcript: Transcript) -> float:
    """Best-effort video duration in seconds (may be 0 for time-less input)."""
    last_end = 0.0
    for utterance in transcript.utterances:
        end = utterance.end_sec if utterance.end_sec is not None else utterance.start_sec
        if end is not None and end > last_end:
            last_end = end
    return last_end


def run_pipeline(
    *,
    video_path: Path,
    options: TranscribeOptions,
    output_dir: Optional[Path],
    title_style: ChapterTitleStyle = "keywords",
    clean_mode: CleanMode = "rule-based",
    write_raw_file: bool = True,
    write_clean_file: bool = True,
    write_summary_file: bool = True,
    extractor: AudioExtractor,
    transcriber: Transcriber,
    summarizer: Summarizer,
    clean_writer: Optional[CleanTranscriptWriter] = None,
    raw_writer: Optional[RawTranscriptWriter] = None,
    summary_writer: Optional[SummaryWriter] = None,
    progress_fn: Optional[ProgressFn] = None,
    logger_fn: Optional[LoggerFn] = None,
    model_name: Optional[str] = None,
) -> PipelineResult:
    """Run the full single-file pipeline.

    Parameters are intentionally explicit: callers pass already-constructed
    services so this function never reaches into providers or loggers itself.
    """

    clean_writer = clean_writer or CleanTranscriptWriter()
    raw_writer = raw_writer or RawTranscriptWriter()
    summary_writer = summary_writer or SummaryWriter()
    logger = logger_fn or (lambda _msg: None)
    chosen_model = model_name or _default_model_name(options)

    with tempfile.TemporaryDirectory(prefix="vte_") as temp_dir:
        wav_path = Path(temp_dir) / f"{video_path.stem}.wav"

        logger(f"extract_audio: {video_path.name}")
        extractor.extract(video_path=video_path, output_wav_path=wav_path)

        logger(f"transcribe: model={chosen_model} profile={options.profile}")
        raw_transcript: Transcript = transcriber.transcribe(
            audio_path=wav_path,
            model=chosen_model,
            profile=options.profile,
            language=options.language,
            timeout_sec=options.timeout_sec,
            progress_callback=progress_fn,
        )

    # Write the raw file FIRST so the verbatim artefact always exists even if
    # cleanup or summarization fails later. The raw file is the ground truth.
    raw_transcript_path: Optional[Path] = None
    if write_raw_file:
        raw_transcript_path = raw_writer.write(
            source_video=video_path,
            transcript=raw_transcript,
            output_dir=output_dir,
        )
        logger(f"wrote_raw_transcript: {raw_transcript_path}")

    # Run cleanup exactly once. Both the clean.md file and the summarizer
    # must see the same post-cleanup transcript so chapter boundaries and
    # summary text agree.
    logger(f"cleanup: mode={clean_mode}")
    cleaned_transcript = clean_writer.cleaned_transcript(
        raw_transcript,
        clean_mode=clean_mode,
        language=options.language,
    )

    chapters = (
        build_chapters_for_transcript(cleaned_transcript, title_style=title_style)
        if options.include_chapters
        else []
    )

    summary_result: Optional[SummaryResult] = None
    if options.summary.mode != "none" and chapters:
        logger(f"summarize: mode={options.summary.mode} chapters={len(chapters)}")
        summary_result = summarizer.summarize(
            transcript=cleaned_transcript,
            chapters=chapters,
            language=options.language,
        )

    refined_titles = refined_titles_from_summary(summary_result)

    transcript_path: Optional[Path] = None
    if write_clean_file:
        # cleaned_transcript was already produced above; pass it as `raw` so
        # the writer just formats (clean_mode="raw" means "no more cleanup").
        transcript_path = clean_writer.write(
            source_video=video_path,
            transcript=cleaned_transcript,
            output_dir=output_dir,
            clean_mode="raw",
            include_chapters=options.include_chapters,
            refined_titles=refined_titles,
            title_style=title_style,
            language=options.language,
        )
        logger(f"wrote_clean_transcript: {transcript_path}")

    summary_path: Optional[Path] = None
    if write_summary_file and summary_result is not None:
        summary_path = summary_writer.write(
            source_video=video_path,
            chapters=chapters,
            summary_result=summary_result,
            output_dir=output_dir,
        )
        if summary_path is not None:
            logger(f"wrote_summary: {summary_path}")

    return PipelineResult(
        transcript_path=transcript_path,
        raw_transcript_path=raw_transcript_path,
        summary_path=summary_path,
        duration_seconds=_duration_from_transcript(cleaned_transcript),
        chapter_count=len(chapters),
        utterance_count=len(cleaned_transcript.utterances),
        trail_loop_dropped=int(getattr(transcriber, "last_trail_loop_trim", 0) or 0),
    )


def _default_model_name(options: TranscribeOptions) -> str:
    """Mirror the CLI default-model policy.

    Kept private so callers don't accidentally reimplement it.
    """
    if options.model:
        return options.model
    return "small" if options.profile == "fast" else "large-v3"
