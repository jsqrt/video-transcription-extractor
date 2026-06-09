"""Writes the human-readable ``<video>.transcription.md`` file.

The output is a flat sequence of utterance paragraphs — no chapter
headings, no titles. Chaptering was removed entirely: previous versions
tried to derive chapters with a frequency-based heuristic, but the
output rarely matched what users expected and the LLM-driven summary
already covers that role through its ``## By Section`` block.

Three ``clean_mode`` values still apply to the utterance stream itself:

* ``"raw"``        — no cleanup; utterances exactly as Whisper produced them.
* ``"rule-based"`` (default) — apply :func:`rule_based_cleanup` first.
* ``"llm"``        — rule-based pass + a conservative LLM polish validated
                     by :func:`is_word_subsequence`; falls back to
                     rule-based on any failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from app.models.types import Transcript
from app.services.cleanup import (
    CleanMode,
    llm_cleanup,
    rule_based_cleanup,
)


def transcript_to_clean_markdown(transcript: Transcript) -> str:
    """Render a transcript as flat markdown paragraphs.

    The transcript passed in is assumed to have already been cleaned by
    the caller (rule-based / LLM / raw). This function only formats.
    """
    if not transcript.utterances:
        return ""
    return _paragraphs_from_utterances(transcript.utterances) + "\n"


# Friendly labels assigned to diarization speaker tags when the
# transcript has 2+ speakers. Single-speaker recordings render without
# any label so the prose stays uncluttered.
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
        language: Optional[str] = None,
    ) -> Path:
        cleaned = self._apply_cleanup(
            transcript, mode=clean_mode, language=language
        )
        target_dir = output_dir if output_dir else source_video.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / f"{source_video.stem}.transcription.md"
        output_path.write_text(
            transcript_to_clean_markdown(cleaned),
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


__all__ = [
    "CleanTranscriptWriter",
    "transcript_to_clean_markdown",
]
