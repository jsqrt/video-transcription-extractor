from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from app.models.types import AudioExtractionError


@lru_cache(maxsize=1)
def _resolve_ffmpeg_binary() -> str | None:
    """Return an absolute path to an ffmpeg binary, or ``None``.

    Resolution order:

    1. ``imageio_ffmpeg.get_ffmpeg_exe()`` — packaged with the GUI build so
       end users get a working binary without installing anything.
    2. ``shutil.which("ffmpeg")`` — system ffmpeg for CLI-only and dev
       environments where imageio-ffmpeg is not installed.
    """
    try:
        import imageio_ffmpeg  # noqa: WPS433 (imported here by design)

        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and Path(path).is_file():
            return path
    except Exception:
        pass
    return shutil.which("ffmpeg")


class AudioExtractor:
    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate

    def ensure_ffmpeg(self) -> str:
        binary = _resolve_ffmpeg_binary()
        if binary is None:
            raise AudioExtractionError(
                "ffmpeg was not found. Install it system-wide and add to "
                "PATH, or install imageio-ffmpeg (`pip install "
                "imageio-ffmpeg`) for a bundled copy."
            )
        return binary

    def extract(self, video_path: Path, output_wav_path: Path) -> Path:
        ffmpeg_binary = self.ensure_ffmpeg()

        # Security hardening:
        # * ``-protocol_whitelist file`` blocks ffmpeg's ``concat:``,
        #   ``http:``, ``crypto:`` and other protocols. Without this, a
        #   file literally named ``concat:/etc/passwd|...mp4`` would be
        #   parsed by ffmpeg as a concat protocol string and could leak
        #   the contents of unrelated files into the wav output.
        # * Prefixing the input with ``file:`` forces the file protocol
        #   regardless of how the path itself looks, removing any chance
        #   of ffmpeg auto-detecting another protocol from the prefix.
        command = [
            ffmpeg_binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file",
            "-i",
            f"file:{video_path}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(self.sample_rate),
            str(output_wav_path),
        ]

        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
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
