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

    def error(self, message: str) -> None:
        print(f"[error] {message}")
