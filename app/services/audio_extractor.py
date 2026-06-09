from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from app.models.types import AudioExtractionError


def _bundled_ffmpeg_search_dirs() -> list[Path]:
    """Where might a bundled ffmpeg live in a frozen build?

    PyInstaller puts ``imageio_ffmpeg`` and its ``binaries/`` folder
    under ``_internal/`` (on --onedir) or under ``sys._MEIPASS`` (on
    --onefile / first-time extraction). The plain
    ``imageio_ffmpeg.get_ffmpeg_exe()`` import path can break in those
    layouts because the wheel's ``__file__`` resolution lands on a
    different parent than the binaries.
    """
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        roots += [exe_dir, exe_dir / "_internal", exe_dir.parent / "Resources"]
    dirs: list[Path] = []
    for r in roots:
        dirs.append(r / "imageio_ffmpeg" / "binaries")
    return dirs


def _find_bundled_ffmpeg() -> str | None:
    """Look for an ``ffmpeg*`` binary inside the frozen bundle."""
    for d in _bundled_ffmpeg_search_dirs():
        if not d.is_dir():
            continue
        for entry in d.iterdir():
            name = entry.name.lower()
            if not entry.is_file():
                continue
            if not name.startswith("ffmpeg"):
                continue
            # On Windows the wheel ships ffmpeg-win-x86_64-vX.Y.exe;
            # on macOS / Linux it's an unsuffixed Mach-O / ELF file.
            if sys.platform == "win32" and not name.endswith(".exe"):
                continue
            return str(entry)
    return None


@lru_cache(maxsize=1)
def _resolve_ffmpeg_binary() -> str | None:
    """Return an absolute path to an ffmpeg binary, or ``None``.

    Resolution order:

    1. Explicit override via ``IMAGEIO_FFMPEG_EXE`` env var. Useful for
       advanced users who want a system ffmpeg even in a frozen build.
    2. PyInstaller bundle search — we look in the same locations the
       PyInstaller hook copies ``imageio_ffmpeg/binaries/`` to.
    3. ``imageio_ffmpeg.get_ffmpeg_exe()`` — the official API, works
       reliably in dev envs and most frozen layouts.
    4. ``shutil.which("ffmpeg")`` — system ffmpeg from PATH.
    """
    override = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if override and Path(override).is_file():
        return override

    bundled = _find_bundled_ffmpeg()
    if bundled:
        return bundled

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
