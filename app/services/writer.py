from __future__ import annotations

from pathlib import Path

from app.models.types import Transcript


def transcript_to_text(transcript: Transcript) -> str:
    return "\n".join(f"[{u.speaker}] {u.text}" for u in transcript.utterances)


class TranscriptWriter:
    def write(
        self,
        source_video: Path,
        transcript: Transcript,
        output_dir: Path | None,
    ) -> Path:
        target_dir = output_dir if output_dir else source_video.parent
        target_dir.mkdir(parents=True, exist_ok=True)

        output_path = target_dir / f"{source_video.stem}.transcript.txt"
        output_path.write_text(transcript_to_text(transcript), encoding="utf-8")
        return output_path
