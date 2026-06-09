from __future__ import annotations

from pathlib import Path


class CliLogger:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def status(self, video_path: Path, state: str) -> None:
        print(f"[{state}] {video_path}")

    def info(self, message: str) -> None:
        if self.verbose:
            print(f"[info] {message}")

    def warn(self, message: str) -> None:
        # Warnings are always visible regardless of ``verbose`` — they signal
        # lost data (e.g. Whisper trail-loop) or degraded fallbacks the user
        # needs to know about.
        print(f"[warn] {message}")

    def error(self, message: str) -> None:
        print(f"[error] {message}")
