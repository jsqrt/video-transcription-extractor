"""High-level pipeline wrapper shared by the CLI, the MCP server, and the GUI.

All three frontends perform exactly the same sequence of steps for a single
media file:

    extract audio → transcribe → cleanup → summarize → write files.

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

from app.models.types import (
    ModelNotFoundError,
    ProviderUnavailableError,
    SummarizationError,
    SummarizationTimeoutError,
    Transcript,
    TranscribeOptions,
    Utterance,
)
from app.services.audio_extractor import AudioExtractor
from app.services.cleanup import CleanMode
from app.services.summarizer import Summarizer
from app.services.summary_writer import IncrementalSummaryFile, SummaryWriter
from app.services.transcriber import Transcriber
from app.services.writer import CleanTranscriptWriter
from app.services.diarizer import diarize


@dataclass(frozen=True)
class PipelineResult:
    """Everything a caller (CLI / MCP / GUI / tests) typically wants to report."""

    transcript_path: Optional[Path]  # <stem>.transcription.md
    summary_path: Optional[Path]  # <stem>.summary.md
    duration_seconds: float
    utterance_count: int
    # Number of trailing lines trimmed because Whisper entered a
    # repeat-loop at end-of-audio. Non-zero means audio content was
    # likely lost in that region. Zero on healthy transcripts.
    trail_loop_dropped: int = 0
    # ISO-639-1 code Whisper detected for this file (e.g. "uk", "ru").
    # ``None`` when detection didn't run or fell back to per-segment auto.
    detected_language: Optional[str] = None
    detected_language_probability: Optional[float] = None
    # Populated when summarization was requested but failed (LLM down,
    # OOM, timeout, etc). The transcription artefact is independent and
    # is still produced; the caller surfaces this to the user so they
    # know WHY the .summary.md file is missing.
    summary_error: Optional[str] = None


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
    clean_mode: CleanMode = "rule-based",
    write_clean_file: bool = True,
    write_summary_file: bool = True,
    extractor: AudioExtractor,
    transcriber: Transcriber,
    summarizer: Summarizer,
    clean_writer: Optional[CleanTranscriptWriter] = None,
    summary_writer: Optional[SummaryWriter] = None,
    progress_fn: Optional[ProgressFn] = None,
    summary_progress_fn: Optional[ProgressFn] = None,
    logger_fn: Optional[LoggerFn] = None,
    model_name: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
) -> PipelineResult:
    """Run the full single-file pipeline.

    Parameters are intentionally explicit: callers pass already-constructed
    services so this function never reaches into providers or loggers itself.

    ``progress_fn`` receives transcription progress (0→1); the optional
    ``summary_progress_fn`` separately receives summarization progress
    (0→1) so a GUI can show one bar per stage. ``cancel_event`` lets a
    GUI / long-running caller request cooperative cancellation between
    stages and (when the Transcriber supports it) inside the transcription
    loop itself.
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

        prompt_preview = (
            options.initial_prompt[:60] + "…"
            if options.initial_prompt and len(options.initial_prompt) > 60
            else (options.initial_prompt or "")
        )
        logger(
            f"transcribe: model={chosen_model} profile={options.profile}"
            + (f" prompt={prompt_preview!r}" if prompt_preview else "")
        )
        raw_transcript: Transcript = transcriber.transcribe(
            audio_path=wav_path,
            model=chosen_model,
            profile=options.profile,
            language=options.language,
            timeout_sec=options.timeout_sec,
            progress_callback=progress_fn,
            cancel_event=cancel_event,
            initial_prompt=options.initial_prompt,
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
            # writer.py remaps raw diarization tags to Alpha/Beta/… for
            # transcripts with 2+ speakers, so we carry the raw label
            # through here (or "" when no segment overlapped this utterance).
            speaker = str(best_label) if best_label is not None else ""

            new_utts.append(
                Utterance(speaker=speaker, text=utt.text, start_sec=utt.start_sec, end_sec=utt.end_sec)
            )
        cleaned_transcript = Transcript(
            utterances=tuple(new_utts),
            detected_language=cleaned_transcript.detected_language,
            detected_language_probability=cleaned_transcript.detected_language_probability,
        )

    # Write the transcript file IMMEDIATELY — the moment transcription and
    # cleanup are done, before the (slower) summary stage runs. The user
    # gets the .transcription.md right away, and if they cancel during
    # summarization it is already safely on disk.
    transcript_path: Optional[Path] = None
    if write_clean_file:
        transcript_path = clean_writer.write(
            source_video=video_path,
            transcript=cleaned_transcript,
            output_dir=output_dir,
            clean_mode="raw",
            language=options.language,
        )
        logger(f"wrote_clean_transcript: {transcript_path}")

    # Summary is produced as ready-to-render markdown by the LLM, one chunk
    # at a time. Each chunk's summary is streamed to the .summary.md file as
    # it is produced (written after the first chunk, appended after each),
    # so a cancel mid-summary keeps everything generated so far. If the LLM
    # is unavailable or fails we surface the error and skip the rest, but the
    # transcription file (above) is independent and already written.
    summary_markdown: Optional[str] = None
    summary_error: Optional[Exception] = None
    summary_path: Optional[Path] = None
    # If the user already cancelled (between transcription and summary), skip
    # summarization but DON'T raise: the transcript is on disk and we return
    # its path so the caller can link it and mark the job cancelled.
    if options.summary.mode != "none" and not (
        cancel_event is not None and cancel_event.is_set()
    ):
        # Prefer the language the user forced. Otherwise hand the
        # summarizer Whisper's detected language: a text-only LLM is
        # bad at telling Ukrainian from Russian on Cyrillic, but
        # Whisper has acoustic cues and gets it right.
        summary_language = options.language or cleaned_transcript.detected_language
        if (
            options.language is None
            and cleaned_transcript.detected_language is not None
        ):
            probability = cleaned_transcript.detected_language_probability
            prob_str = f" (p={probability:.2f})" if probability is not None else ""
            logger(
                f"summary language: using detected "
                f"'{cleaned_transcript.detected_language}'{prob_str}"
            )
        logger(f"summarize: mode={options.summary.mode}")
        # Stream each chunk's summary straight to <stem>.summary.md as it is
        # produced. ``incremental`` owns the file; ``partial_callback`` hands
        # it each chunk. On cancel/crash the file keeps every streamed chunk.
        incremental = (
            IncrementalSummaryFile(source_video=video_path, output_dir=output_dir)
            if write_summary_file
            else None
        )
        try:
            summary_markdown = summarizer.summarize(
                transcript=cleaned_transcript,
                language=summary_language,
                progress_callback=summary_progress_fn,
                partial_callback=(incremental.append if incremental else None),
                cancel_event=cancel_event,
            )
        except (
            ModelNotFoundError,
            ProviderUnavailableError,
            SummarizationTimeoutError,
            SummarizationError,
        ) as exc:
            summary_error = exc
            logger(f"summary failed: {exc}")
        # If chunks were streamed but the run ended early (error or cancel)
        # without a clean finalize, the partial file still stands — point the
        # result at it so the user sees what was produced.
        if incremental is not None and incremental.started:
            summary_path = incremental.path

    # (The transcript file was already written above, before summarization.)

    # Finalize the summary with a canonical write on clean completion — this
    # overwrites the streamed file with the exact title + body (BOM-correct).
    # On cancel/error ``summary_markdown`` is the partial digest or None and
    # the streamed file already stands (summary_path was set during the run).
    if write_summary_file and summary_markdown:
        finalized = summary_writer.write(
            source_video=video_path,
            summary_markdown=summary_markdown,
            output_dir=output_dir,
        )
        if finalized is not None:
            summary_path = finalized
            logger(f"wrote_summary: {summary_path}")

    return PipelineResult(
        transcript_path=transcript_path,
        summary_path=summary_path,
        duration_seconds=_duration_from_transcript(cleaned_transcript),
        utterance_count=len(cleaned_transcript.utterances),
        trail_loop_dropped=int(getattr(transcriber, "last_trail_loop_trim", 0) or 0),
        detected_language=cleaned_transcript.detected_language,
        detected_language_probability=cleaned_transcript.detected_language_probability,
        summary_error=str(summary_error) if summary_error is not None else None,
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
