"""Writes the ``<video>.summary.md`` artefact.

The :class:`Summarizer` now returns ready-to-render markdown (the LLM
builds the whole document itself), so this writer's only job is to
prepend a ``# Summary: <video>`` title and persist the bytes. No
parsing, no field validation, no section ordering — the LLM is the
sole author of the document body.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def summary_to_markdown(video_name: str, summary_markdown: str) -> str:
    """Wrap LLM-produced markdown with a top-level title.

    The LLM emits its own ``## Overview`` / ``## Огляд`` headings, so
    we only add the H1 title above them.
    """
    body = (summary_markdown or "").strip()
    if not body:
        return f"# Summary: {video_name}\n\n_No summary generated._\n"
    return f"# Summary: {video_name}\n\n{body}\n"


class IncrementalSummaryFile:
    """Streams a summary to ``<stem>.summary.md`` as chunks arrive.

    The first chunk writes the title + that chunk (with a BOM so Windows
    opens it correctly); each later chunk is appended after a blank-line
    seam. The point is durability: if the user cancels mid-summary, or the
    process is killed, the file already holds every chunk produced so far —
    nothing is lost. The pipeline still does a final canonical write on
    clean completion, which simply overwrites this same path.
    """

    def __init__(self, source_video: Path, output_dir: Optional[Path] = None) -> None:
        self._dir = output_dir if output_dir else source_video.parent
        self._name = source_video.stem
        self.path = self._dir / f"{source_video.stem}.summary.md"
        self.started = False

    def append(self, text: str) -> None:
        body = (text or "").strip()
        if not body:
            return
        if not self.started:
            self._dir.mkdir(parents=True, exist_ok=True)
            # First write carries the title and a BOM (utf-8-sig).
            self.path.write_text(
                f"# Summary: {self._name}\n\n{body}\n", encoding="utf-8-sig"
            )
            self.started = True
        else:
            # Appends are plain utf-8 — utf-8-sig would re-emit a BOM
            # mid-file. A leading blank line keeps blocks separated.
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n{body}\n")


class SummaryWriter:
    """Persists raw LLM markdown as ``<stem>.summary.md``."""

    def write(
        self,
        source_video: Path,
        summary_markdown: Optional[str],
        output_dir: Optional[Path] = None,
    ) -> Optional[Path]:
        if not summary_markdown or not summary_markdown.strip():
            return None

        target_dir = output_dir if output_dir else source_video.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / f"{source_video.stem}.summary.md"
        output_path.write_text(
            summary_to_markdown(
                video_name=source_video.stem,
                summary_markdown=summary_markdown,
            ),
            encoding="utf-8-sig",  # BOM so Windows opens correctly
        )
        return output_path


__all__ = ["SummaryWriter", "IncrementalSummaryFile", "summary_to_markdown"]
