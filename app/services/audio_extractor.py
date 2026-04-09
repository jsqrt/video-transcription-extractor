from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.models.types import AudioExtractionError


class AudioExtractor:
    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate

    def ensure_ffmpeg(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise AudioExtractionError(
                "ffmpeg was not found in PATH. Install ffmpeg and add it to PATH."
            )

    def extract(self, video_path: Path, output_wav_path: Path) -> Path:
        self.ensure_ffmpeg()

        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(self.sample_rate),
            str(output_wav_path),
        ]

        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or "unknown error").strip()
            raise AudioExtractionError(
                f"Failed to extract audio from {video_path.name}: {details}"
            ) from exc

        if not output_wav_path.exists():
            raise AudioExtractionError(
                f"ffmpeg completed without error, but output file was not created: {output_wav_path}"
            )

        return output_wav_path
