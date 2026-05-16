"""Unit tests for the clean-markdown transcript writer and summary writer."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.types import SummaryOptions, Transcript, Utterance
from app.services.chapterizer import build_chapters
from app.services.summarizer import (
    ChapterSummary,
    Fact,
    Intent,
    Summarizer,
    SummaryResult,
)
from app.services.summary_writer import SummaryWriter, summary_to_markdown
from app.services.writer import (
    CleanTranscriptWriter,
    refined_titles_from_summary,
    transcript_to_clean_markdown,
)


def _utterances(sentences: list[str]) -> tuple[Utterance, ...]:
    return tuple(
        Utterance(
            speaker="SPEAKER_1",
            text=text,
            start_sec=i * 1.0,
            end_sec=i * 1.0 + 1.0,
        )
        for i, text in enumerate(sentences)
    )


class TranscriptToCleanMarkdownTest(unittest.TestCase):
    def test_flat_output_no_chapters(self) -> None:
        transcript = Transcript(
            utterances=_utterances(["one two three", "four five six"])
        )
        text = transcript_to_clean_markdown(transcript, include_chapters=False)
        self.assertIn("one two three", text)
        self.assertIn("four five six", text)
        # No chapter headings and no speaker tags in the clean markdown.
        self.assertNotIn("## ", text)
        self.assertNotIn("[SPEAKER_", text)

    def test_chapter_headings_are_included_when_requested(self) -> None:
        transcript = Transcript(
            utterances=_utterances(
                [
                    "космос планета ракета земля орбіта",
                    "космос планета ракета земля орбіта",
                    "космос планета ракета земля орбіта",
                    "футбол гол стадіон тренер команда",
                    "футбол гол стадіон тренер команда",
                    "футбол гол стадіон тренер команда",
                ]
                * 8
            )
        )
        text = transcript_to_clean_markdown(transcript, include_chapters=True)
        # Every chapter heading uses `## ` (level-2) so headings aren't
        # confused with the top-level "# Transcript" section.
        self.assertIn("## ", text)
        self.assertIn("Chapter 01", text)

    def test_refined_titles_override_chapter_heading(self) -> None:
        transcript = Transcript(
            utterances=_utterances(
                [
                    "космос планета ракета земля орбіта",
                    "космос планета ракета земля орбіта",
                    "космос планета ракета земля орбіта",
                ]
                * 6
            )
        )
        text = transcript_to_clean_markdown(
            transcript,
            include_chapters=True,
            refined_titles={1: "Про космос"},
        )
        self.assertIn("Chapter 01: Про космос", text)


class CleanTranscriptWriterTest(unittest.TestCase):
    def test_writer_writes_clean_md_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_video = root / "myclip.mp4"
            fake_video.write_bytes(b"data")
            transcript = Transcript(
                utterances=_utterances(["hello world", "second line"])
            )

            out = CleanTranscriptWriter().write(
                source_video=fake_video,
                transcript=transcript,
                output_dir=root,
                include_chapters=False,
                clean_mode="raw",
            )

            self.assertEqual(out.name, "myclip.clean.md")
            content = out.read_text(encoding="utf-8")
            self.assertIn("hello world", content)
            self.assertIn("second line", content)

    def test_rule_based_mode_deduplicates_repeats(self) -> None:
        # Two identical utterances within the dup window must collapse to one.
        transcript = Transcript(
            utterances=(
                Utterance(
                    speaker="SPEAKER_1",
                    text="Це велике відкриття.",
                    start_sec=0.0,
                    end_sec=2.0,
                ),
                Utterance(
                    speaker="SPEAKER_1",
                    text="Це велике відкриття.",
                    start_sec=2.1,
                    end_sec=4.0,
                ),
                Utterance(
                    speaker="SPEAKER_1",
                    text="А тепер продовжуємо.",
                    start_sec=4.1,
                    end_sec=6.0,
                ),
            )
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_video = root / "clip.mp4"
            fake_video.write_bytes(b"data")
            out = CleanTranscriptWriter().write(
                source_video=fake_video,
                transcript=transcript,
                output_dir=root,
                include_chapters=False,
                clean_mode="rule-based",
            )
            content = out.read_text(encoding="utf-8")
        # The duplicate is dropped, so the string appears only once.
        self.assertEqual(content.count("Це велике відкриття."), 1)
        self.assertIn("А тепер продовжуємо.", content)


class SummaryWriterTest(unittest.TestCase):
    def test_summary_markdown_has_all_four_sections(self) -> None:
        transcript = Transcript(
            utterances=_utterances(
                [
                    "нафта ринок торгівля ормузька протока іран трамп",
                    "нафта ринок торгівля ормузька протока іран трамп",
                    "нафта ринок торгівля ормузька протока іран трамп",
                    "футбол гол стадіон команда матч тренер",
                    "футбол гол стадіон команда матч тренер",
                    "футбол гол стадіон команда матч тренер",
                ]
                * 6
            )
        )
        chapters = build_chapters(transcript)
        summary = Summarizer(
            options=SummaryOptions(
                mode="extractive",
                per_chapter_sentences=2,
                overview_sentences=3,
            )
        ).summarize(transcript=transcript, chapters=chapters)

        markdown = summary_to_markdown(
            video_name="myclip",
            chapters=chapters,
            summary_result=summary,
        )
        self.assertIn("# Summary: myclip", markdown)
        self.assertIn("## Overview", markdown)
        self.assertIn("## По чаптерах", markdown)
        for index in range(1, len(chapters) + 1):
            self.assertIn(f"Chapter {index:02d}", markdown)

    def test_summary_markdown_renders_facts_and_intents(self) -> None:
        chapters = []  # per-chapter section is optional when there are chapters
        summary = SummaryResult(
            overview="Короткий огляд.",
            key_facts=(
                Fact(text="Нафта $100 за барель", timecode="[00:12]"),
                Fact(text="4 кораблі у протоці", timecode=None),
            ),
            intents=(
                Intent(
                    text="США планують оголосити нові умови",
                    timecode="[01:30]",
                ),
            ),
        )
        markdown = summary_to_markdown(
            video_name="myclip",
            chapters=chapters,
            summary_result=summary,
        )
        self.assertIn("## Ключові факти", markdown)
        self.assertIn("[00:12] Нафта $100 за барель", markdown)
        self.assertIn("- 4 кораблі у протоці", markdown)
        self.assertIn("## Наміри / дії", markdown)
        self.assertIn("[01:30] США планують оголосити нові умови", markdown)

    def test_summary_writer_skips_when_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_video = root / "myclip.mp4"
            fake_video.write_bytes(b"data")
            result = SummaryWriter().write(
                source_video=fake_video,
                chapters=[],
                summary_result=SummaryResult(overview=""),
                output_dir=root,
            )
        self.assertIsNone(result)

    def test_summary_writer_writes_expected_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_video = root / "myclip.mp4"
            fake_video.write_bytes(b"data")
            transcript = Transcript(
                utterances=_utterances(
                    [
                        "alpha beta gamma delta epsilon zeta eta theta",
                        "alpha beta gamma delta epsilon zeta eta theta",
                        "alpha beta gamma delta epsilon zeta eta theta",
                    ]
                    * 5
                )
            )
            chapters = build_chapters(transcript)
            summary = SummaryResult(
                overview="High level overview.",
                per_chapter=tuple(
                    ChapterSummary(
                        chapter_index=index,
                        refined_title=f"Refined {index:02d}",
                        summary=f"Summary for chapter {index}.",
                    )
                    for index in range(1, len(chapters) + 1)
                ),
            )
            path = SummaryWriter().write(
                source_video=fake_video,
                chapters=chapters,
                summary_result=summary,
                output_dir=root,
            )
            self.assertIsNotNone(path)
            assert path is not None
            self.assertEqual(path.name, "myclip.summary.md")
            content = path.read_text(encoding="utf-8")
            self.assertIn("High level overview.", content)
            self.assertIn("Refined 01", content)


class RefinedTitlesHelperTest(unittest.TestCase):
    def test_builds_mapping_from_summary_result(self) -> None:
        summary = SummaryResult(
            overview="",
            per_chapter=(
                ChapterSummary(
                    chapter_index=1, refined_title="Перша", summary="one"
                ),
                ChapterSummary(
                    chapter_index=2, refined_title="", summary="no title"
                ),
                ChapterSummary(
                    chapter_index=3, refined_title="Третя", summary="three"
                ),
            ),
        )
        self.assertEqual(
            refined_titles_from_summary(summary),
            {1: "Перша", 3: "Третя"},
        )

    def test_returns_empty_when_summary_is_none(self) -> None:
        self.assertEqual(refined_titles_from_summary(None), {})


if __name__ == "__main__":
    unittest.main()
