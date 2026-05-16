"""Writes ``<video>.raw.txt`` — the verbatim Whisper output.

This file is the *ground truth*:

* No sanitizing, no dedup, no sentence stitching.
* One utterance per line.
* Every line starts with the utterance's start timecode in
  ``[MM:SS]`` (or ``[HH:MM:SS]`` when the video is longer than an hour).
* No speaker labels, no chapters, no markdown.

Consumers:

* Humans reading the raw model output as the ultimate source of truth.
* The cleanup layer, which uses this file as a reference when debugging
  whether an aggressive rule has removed something real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.models.types import Transcript


def _format_timecode(seconds: Optional[float], *, force_hours: bool) -> str:
    if seconds is None:
        return "[--:--]"
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if force_hours or hours > 0:
        return f"[{hours:02d}:{minutes:02d}:{secs:02d}]"
    return f"[{minutes:02d}:{secs:02d}]"


def transcript_to_raw_text(transcript: Transcript) -> str:
    """Return the raw `[MM:SS] text` line-per-utterance document.

    Whether the whole file uses ``[HH:MM:SS]`` is decided once up front
    so lines stay column-aligned.
    """
    utterances = transcript.utterances
    if not utterances:
        return ""

    max_end = 0.0
    for utt in utterances:
        end = utt.end_sec if utt.end_sec is not None else (utt.start_sec or 0.0)
        if end is not None and end > max_end:
            max_end = end

    force_hours = max_end >= 3600.0

    lines: list[str] = []
    for utt in utterances:
        text = utt.text.strip()
        if not text:
            continue
        lines.append(f"{_format_timecode(utt.start_sec, force_hours=force_hours)} {text}")
    return "\n".join(lines) + ("\n" if lines else "")


class RawTranscriptWriter:
    """Persists a :class:`Transcript` as ``<stem>.raw.txt``."""

    def write(
        self,
        source_video: Path,
        transcript: Transcript,
        output_dir: Optional[Path] = None,
    ) -> Path:
        target_dir = output_dir if output_dir else source_video.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / f"{source_video.stem}.raw.txt"
        output_path.write_text(
            transcript_to_raw_text(transcript), encoding="utf-8"
        )
        return output_path


__all__ = ["RawTranscriptWriter", "transcript_to_raw_text"]
