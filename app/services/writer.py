"""Writes the human-readable ``<video>.transcription.md`` file.

There are three ``clean_mode`` values:

* ``"raw"``   — no cleanup; chapters + utterances as Whisper produced them.
                (Distinct from the ``.raw.txt`` file: this one still has
                chapter headings and markdown structure.)
* ``"rule-based"`` (default) — apply :func:`rule_based_cleanup` first.
* ``"llm"``   — rule-based pass + a conservative LLM polish validated by
                :func:`is_word_subsequence`. Falls back to rule-based on
                any failure.

Chapters are re-computed from the cleaned transcript so their titles and
boundaries reflect the post-cleanup text, not the raw Whisper chunks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Sequence

from app.models.types import Transcript
from app.services.chapterizer import (
    Chapter,
    ChapterTitleStyle,
    build_chapters,
    format_timecode_bracketed,
)
from app.services.cleanup import (
    CleanMode,
    llm_cleanup,
    rule_based_cleanup,
)


def _chapter_heading(
    chapter: Chapter, index: int, refined_title: Optional[str]
) -> str:
    timecode = format_timecode_bracketed(chapter.start_sec)
    prefix = f"{timecode} " if timecode else ""
    if refined_title:
        return f"{prefix}Chapter {index:02d}: {refined_title}"
    raw = chapter.title
    if ": " in raw:
        return f"{prefix}Chapter {index:02d}: {raw.split(': ', 1)[1]}"
    return f"{prefix}Chapter {index:02d}: {raw}"


def transcript_to_clean_markdown(
    transcript: Transcript,
    *,
    include_chapters: bool = True,
    refined_titles: Optional[dict[int, str]] = None,
    title_style: ChapterTitleStyle = "keywords",
) -> str:
    """Render a transcript as markdown with chapter headings.

    The transcript passed in is assumed to have already been cleaned by
    the caller (rule-based / LLM / raw). This function only formats.
    """
    utterances = transcript.utterances

    if not include_chapters or not utterances:
        return _utterances_to_paragraphs(transcript) + ("\n" if utterances else "")

    chapters = build_chapters(transcript, title_style=title_style)
    if not chapters:
        return _utterances_to_paragraphs(transcript) + "\n"

    refined_titles = refined_titles or {}
    lines: list[str] = []
    for index, chapter in enumerate(chapters, start=1):
        heading = _chapter_heading(chapter, index, refined_titles.get(index))
        lines.append(f"## {heading}")
        lines.append("")
        chapter_utterances = utterances[chapter.start_index:chapter.end_index]
        lines.append(_paragraphs_from_utterances(chapter_utterances))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _utterances_to_paragraphs(transcript: Transcript) -> str:
    return _paragraphs_from_utterances(transcript.utterances)


_SPEAKER_LABELS = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta"]


def _paragraphs_from_utterances(utterances) -> str:
    unique_speakers: list[str] = []
    seen: set[str] = set()
    for utt in utterances:
        sp = getattr(utt, "speaker", None) or ""
        if sp and sp != "UNKNOWN_SPEAKER" and sp not in seen:
            seen.add(sp)
            unique_speakers.append(sp)

    speaker_map: dict[str, str] = {}
    if len(unique_speakers) >= 2:
        for i, sp in enumerate(unique_speakers):
            label = _SPEAKER_LABELS[i] if i < len(_SPEAKER_LABELS) else f"Speaker {i + 1}"
            speaker_map[sp] = label

    parts = []
    for utt in utterances:
        text = utt.text.strip()
        if not text:
            continue
        sp = getattr(utt, "speaker", None) or ""
        label = speaker_map.get(sp, "")
        if label:
            parts.append(f"{label}: {text}")
        else:
            parts.append(text)
    return "\n\n".join(parts)


class CleanTranscriptWriter:
    """Persists a cleaned :class:`Transcript` as ``<stem>.transcription.md``.

    Accepts a cleanup mode and delegates the actual cleanup to the
    :mod:`app.services.cleanup` module.
    """

    def __init__(
        self,
        *,
        llm_client=None,
        logger_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._llm_client = llm_client
        self._logger = logger_fn or (lambda _msg: None)

    def write(
        self,
        source_video: Path,
        transcript: Transcript,
        output_dir: Optional[Path] = None,
        *,
        clean_mode: CleanMode = "rule-based",
        include_chapters: bool = True,
        refined_titles: Optional[dict[int, str]] = None,
        title_style: ChapterTitleStyle = "keywords",
        language: Optional[str] = None,
    ) -> Path:
        cleaned = self._apply_cleanup(
            transcript, mode=clean_mode, language=language
        )
        target_dir = output_dir if output_dir else source_video.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / f"{source_video.stem}.transcription.md"
        output_path.write_text(
            transcript_to_clean_markdown(
                cleaned,
                include_chapters=include_chapters,
                refined_titles=refined_titles,
                title_style=title_style,
            ),
            encoding="utf-8",
        )
        return output_path

    # Exposed so the pipeline can reuse the cleaned transcript for the
    # summarizer (no need to run the work twice).
    def cleaned_transcript(
        self,
        transcript: Transcript,
        *,
        clean_mode: CleanMode,
        language: Optional[str] = None,
    ) -> Transcript:
        return self._apply_cleanup(transcript, mode=clean_mode, language=language)

    def _apply_cleanup(
        self,
        transcript: Transcript,
        *,
        mode: CleanMode,
        language: Optional[str],
    ) -> Transcript:
        if mode == "raw":
            return transcript
        if mode == "rule-based":
            return rule_based_cleanup(transcript)
        if mode == "llm":
            result = llm_cleanup(
                transcript,
                llm_client=self._llm_client,
                language=language,
                logger_fn=self._logger,
            )
            if result.used_llm:
                self._logger("clean_mode=llm: LLM polish accepted")
            else:
                self._logger(
                    f"clean_mode=llm: using rule-based ({result.reason})"
                )
            return result.transcript
        raise ValueError(f"unknown clean_mode: {mode!r}")


def build_chapters_for_transcript(
    transcript: Transcript, title_style: ChapterTitleStyle = "keywords"
) -> list[Chapter]:
    return build_chapters(transcript, title_style=title_style)


def refined_titles_from_summary(summary_result) -> dict[int, str]:
    """Build a ``{chapter_index: refined_title}`` map from a SummaryResult."""
    if summary_result is None:
        return {}
    mapping: dict[int, str] = {}
    for chapter_summary in summary_result.per_chapter:
        if chapter_summary.refined_title:
            mapping[chapter_summary.chapter_index] = chapter_summary.refined_title
    return mapping


# Backwards-compat alias for callers still importing the old name.
TranscriptWriter = CleanTranscriptWriter


__all__ = [
    "CleanTranscriptWriter",
    "TranscriptWriter",
    "transcript_to_clean_markdown",
    "build_chapters_for_transcript",
    "refined_titles_from_summary",
]
