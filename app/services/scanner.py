from __future__ import annotations

from pathlib import Path

from app.models.types import ScanError

DEFAULT_EXTENSIONS = {".mp4", ".mov", ".mkv"}


def parse_extensions(raw_ext: str | None) -> set[str]:
    if not raw_ext:
        return set(DEFAULT_EXTENSIONS)

    parsed = {
        f".{item.strip().lower().lstrip('.')}"
        for item in raw_ext.split(",")
        if item.strip()
    }
    if not parsed:
        raise ScanError("Empty --ext value. Example: --ext mp4,mov,mkv")
    return parsed


def scan_videos(input_path: str, allowed_ext: set[str]) -> list[Path]:
    path = Path(input_path).expanduser().resolve()

    if not path.exists():
        raise ScanError(f"Input path not found: {path}")

    if path.is_file():
        if path.suffix.lower() not in allowed_ext:
            raise ScanError(
                f"File {path.name} has unsupported format {path.suffix}."
            )
        return [path]

    if not path.is_dir():
        raise ScanError(f"Path is not a file or directory: {path}")

    files = sorted(
        p for p in path.iterdir() if p.is_file() and p.suffix.lower() in allowed_ext
    )
    if not files:
        ext_text = ", ".join(sorted(e.lstrip(".") for e in allowed_ext))
        raise ScanError(
            f"No video files found in {path} with extensions: {ext_text}"
        )

    return files
