"""Renders and writes the ``<video>.summary.md`` file.

The summary file has exactly four sections, matching the structure built
by :class:`app.services.summarizer.Summarizer`:

1. ``## Overview``           — prose TL;DR (2-4 sentences).
2. ``## Key Facts``          — bullets of concrete facts (numbers, dates,
                                names), each optionally prefixed with a
                                ``[MM:SS]`` timecode.
3. ``## Intents & Actions``  — bullets of actions / predictions /
                                recommendations the speaker made.
4. ``## Per Chapter``        — one short bullet per chapter with a
                                timecode, refined title, and summary.

Empty sections are omitted so a sparse summary doesn't produce empty
headings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from app.services.chapterizer import Chapter, format_timecode_bracketed
from app.services.summarizer import (
    ChapterSummary,
    Fact,
    Intent,
    SummaryResult,
)


def _chapter_index_to_title(
    chapters: Sequence[Chapter], summary_result: SummaryResult
) -> dict[int, ChapterSummary]:
    return {cs.chapter_index: cs for cs in summary_result.per_chapter}


def _fallback_title_from_chapter(chapter: Chapter) -> str:
    raw = chapter.title
    if ": " in raw:
        return raw.split(": ", 1)[1].strip()
    return raw.strip()


def _format_bullet(prefix: Optional[str], text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    if prefix:
        return f"- {prefix} {text}"
    return f"- {text}"


def _render_fact_bullets(facts: Sequence[Fact]) -> list[str]:
    lines: list[str] = []
    for fact in facts:
        bullet = _format_bullet(fact.timecode, fact.text)
        if bullet:
            lines.append(bullet)
    return lines


def _render_intent_bullets(intents: Sequence[Intent]) -> list[str]:
    lines: list[str] = []
    for intent in intents:
        bullet = _format_bullet(intent.timecode, intent.text)
        if bullet:
            lines.append(bullet)
    return lines


def _render_per_chapter_bullets(
    chapters: Sequence[Chapter],
    summary_result: SummaryResult,
) -> list[str]:
    if not chapters:
        return []
    per_chapter = _chapter_index_to_title(chapters, summary_result)
    lines: list[str] = []
    for index, chapter in enumerate(chapters, start=1):
        chapter_summary = per_chapter.get(index)
        if chapter_summary and chapter_summary.refined_title:
            title = chapter_summary.refined_title
        else:
            title = _fallback_title_from_chapter(chapter)
        body = chapter_summary.summary.strip() if chapter_summary else ""
        timecode = (
            format_timecode_bracketed(chapter.start_sec)
            if chapter.start_sec is not None
            else ""
        )
        prefix = f"{timecode} " if timecode else ""
        head = f"{prefix}Chapter {index:02d} — {title}".strip()
        if body:
            lines.append(f"- {head}: {body}")
        else:
            lines.append(f"- {head}")
    return lines


def summary_to_markdown(
    video_name: str,
    chapters: Sequence[Chapter],
    summary_result: SummaryResult,
) -> str:
    has_overview = bool(summary_result.overview.strip())
    has_facts = bool(summary_result.key_facts)
    has_intents = bool(summary_result.intents)
    has_per_chapter = bool(summary_result.per_chapter) and bool(chapters)

    if not (has_overview or has_facts or has_intents or has_per_chapter):
        return f"# Summary: {video_name}\n\n_No summary generated._\n"

    lines: list[str] = [f"# Summary: {video_name}", ""]

    if has_overview:
        lines.append("## Overview")
        lines.append("")
        lines.append(summary_result.overview.strip())
        lines.append("")

    if has_facts:
        lines.append("## Key Facts")
        lines.append("")
        lines.extend(_render_fact_bullets(summary_result.key_facts))
        lines.append("")

    if has_intents:
        lines.append("## Intents & Actions")
        lines.append("")
        lines.extend(_render_intent_bullets(summary_result.intents))
        lines.append("")

    if has_per_chapter:
        lines.append("## Per Chapter")
        lines.append("")
        lines.extend(_render_per_chapter_bullets(chapters, summary_result))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


class SummaryWriter:
    """Persists a :class:`SummaryResult` as ``<stem>.summary.md``."""

    def write(
        self,
        source_video: Path,
        chapters: Sequence[Chapter],
        summary_result: Optional[SummaryResult],
        output_dir: Optional[Path] = None,
    ) -> Optional[Path]:
        if summary_result is None:
            return None
        if (
            not summary_result.overview
            and not summary_result.key_facts
            and not summary_result.intents
            and not summary_result.per_chapter
        ):
            return None

        target_dir = output_dir if output_dir else source_video.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / f"{source_video.stem}.summary.md"
        output_path.write_text(
            summary_to_markdown(
                video_name=source_video.stem,
                chapters=chapters,
                summary_result=summary_result,
            ),
            encoding="utf-8",
        )
        return output_path


__all__ = ["SummaryWriter", "summary_to_markdown"]
