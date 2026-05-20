"""High-level pipeline wrapper shared by the CLI, the MCP server, and the GUI.

All three frontends perform exactly the same sequence of steps for a single
media file:

    extract audio → transcribe → cleanup → chapterize → summarize → write files.

Two artefacts are produced per video:

    * ``<stem>.transcription.md``    — readable markdown after cleanup (if enabled).
* ``<stem>.summary.md``  — four-section summary (if enabled and summary is on).

The cleaned transcript (same one that lands in ``.clean.md``) is what the
summarizer sees, so chapters and summary match the file the user reads.

This function is deliberately dependency-injection friendly: every service
has a default but can be overridden from tests.
"""

from __future__ import annotations

import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.models.types import Transcript, TranscribeOptions
from app.services.audio_extractor import AudioExtractor
from app.services.chapterizer import ChapterTitleStyle
from app.services.cleanup import CleanMode
from app.services.summarizer import Summarizer, SummaryResult
from app.services.summary_writer import SummaryWriter
from app.services.transcriber import Transcriber
from app.services.writer import (
    CleanTranscriptWriter,
    build_chapters_for_transcript,
    refined_titles_from_summary,
)
from app.services.diarizer import diarize


@dataclass(frozen=True)
class PipelineResult:
    """Everything a caller (CLI / MCP / GUI / tests) typically wants to report."""

    transcript_path: Optional[Path]  # <stem>.transcription.md
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
    write_clean_file: bool = True,
    write_summary_file: bool = True,
    extractor: AudioExtractor,
    transcriber: Transcriber,
    summarizer: Summarizer,
    clean_writer: Optional[CleanTranscriptWriter] = None,
    summary_writer: Optional[SummaryWriter] = None,
    progress_fn: Optional[ProgressFn] = None,
    logger_fn: Optional[LoggerFn] = None,
    model_name: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
) -> PipelineResult:
    """Run the full single-file pipeline.

    Parameters are intentionally explicit: callers pass already-constructed
    services so this function never reaches into providers or loggers itself.

    ``cancel_event`` lets a GUI / long-running caller request cooperative
    cancellation between stages and (when the Transcriber supports it)
    inside the transcription loop itself.
    """

    clean_writer = clean_writer or CleanTranscriptWriter()
    summary_writer = summary_writer or SummaryWriter()
    logger = logger_fn or (lambda _msg: None)
    chosen_model = model_name or _default_model_name(options)

    with tempfile.TemporaryDirectory(prefix="vte_") as temp_dir:
        wav_path = Path(temp_dir) / f"{video_path.stem}.wav"

        logger(f"extract_audio: {video_path.name}")
        extractor.extract(video_path=video_path, output_wav_path=wav_path)

        if cancel_event is not None and cancel_event.is_set():
            raise _cancelled()

        logger(f"transcribe: model={chosen_model} profile={options.profile}")
        raw_transcript: Transcript = transcriber.transcribe(
            audio_path=wav_path,
            model=chosen_model,
            profile=options.profile,
            language=options.language,
            timeout_sec=options.timeout_sec,
            progress_callback=progress_fn,
            cancel_event=cancel_event,
        )

        # Optionally run diarization on the extracted wav while it still
        # exists, and attach speaker labels to utterances after cleanup.
        # Must run before the temp dir is torn down.
        diarization_segments = None
        if options.diarize:
            if cancel_event is not None and cancel_event.is_set():
                raise _cancelled()
            try:
                diarization_segments = diarize(str(wav_path))
                logger(f"diarize: found {len(diarization_segments)} segments")
            except Exception as exc:
                logger(f"diarize: failed ({exc})")
                diarization_segments = None

        # Release the Whisper model from GPU immediately so Ollama can
        # claim the VRAM for summarization inference. Defensive against
        # provider/test doubles that don't implement the optional method.
        if hasattr(transcriber, "release"):
            transcriber.release()

    # Run cleanup exactly once. Both the clean.md file and the summarizer
    # must see the same post-cleanup transcript so chapter boundaries and
    # summary text agree.
    logger(f"cleanup: mode={clean_mode}")
    cleaned_transcript = clean_writer.cleaned_transcript(
        raw_transcript,
        clean_mode=clean_mode,
        language=options.language,
    )

    # If diarization happened, assign speaker labels to the cleaned utterances
    if diarization_segments:
        new_utts = []
        for utt in cleaned_transcript.utterances:
            start = utt.start_sec or 0.0
            end = utt.end_sec or start
            best_label = None
            best_overlap = 0.0
            for seg in diarization_segments:
                s = seg.get("start", 0.0)
                e = seg.get("end", 0.0)
                overlap = max(0.0, min(end, e) - max(start, s))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_label = seg.get("speaker")
            if best_label is None:
                speaker = ""
            else:
                # Normalize label to "Speaker N" if it looks generic
                sp = str(best_label)
                if sp.lower().startswith("speaker"):
                    speaker = sp
                else:
                    # map arbitrary labels to Speaker 1..N by hashing
                    speaker = sp
            from app.models.types import Utterance

            new_utts.append(
                Utterance(speaker=speaker, text=utt.text, start_sec=utt.start_sec, end_sec=utt.end_sec)
            )
        cleaned_transcript = Transcript(utterances=tuple(new_utts))

    chapters = (
        build_chapters_for_transcript(cleaned_transcript, title_style=title_style)
        if options.include_chapters
        else []
    )

    summary_result: Optional[SummaryResult] = None
    if options.summary.mode != "none" and chapters:
        if cancel_event is not None and cancel_event.is_set():
            raise _cancelled()
        logger(f"summarize: mode={options.summary.mode} chapters={len(chapters)}")
        summary_result = summarizer.summarize(
            transcript=cleaned_transcript,
            chapters=chapters,
            language=options.language,
        )

    refined_titles = refined_titles_from_summary(summary_result)

    transcript_path: Optional[Path] = None
    if write_clean_file:
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


def _cancelled():
    from app.models.types import TranscriptionTimeoutError

    return TranscriptionTimeoutError("Cancelled by caller")
