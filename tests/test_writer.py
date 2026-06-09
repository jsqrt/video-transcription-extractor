"""Unit tests for the clean-markdown transcript writer and summary writer.

The summary writer is now a thin wrapper around an LLM-produced markdown
string — it only prepends ``# Summary: <video>`` and persists. All
structural rendering / parsing tests that used to live here (against the
removed ExtractiveSummarizer / SummaryResult / refined_titles helpers)
were dropped along with the code they exercised.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.types import Transcript, Utterance
from app.services.summary_writer import SummaryWriter, summary_to_markdown
from app.services.writer import (
    CleanTranscriptWriter,
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
    def test_renders_utterances_as_paragraphs(self) -> None:
        # Chaptering was removed in this refactor; the writer now
        # produces a flat sequence of paragraphs with no ``##`` headings.
        transcript = Transcript(
            utterances=_utterances(["one two three", "four five six"])
        )
        text = transcript_to_clean_markdown(transcript)
        self.assertIn("one two three", text)
        self.assertIn("four five six", text)
        self.assertNotIn("## ", text)
        self.assertNotIn("Chapter", text)

    def test_empty_transcript_returns_empty_string(self) -> None:
        text = transcript_to_clean_markdown(Transcript(utterances=()))
        self.assertEqual(text, "")


class CleanTranscriptWriterTest(unittest.TestCase):
    def test_writes_expected_file_layout(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_video = root / "myclip.mp4"
            fake_video.write_bytes(b"data")
            transcript = Transcript(
                utterances=_utterances(["alpha beta gamma", "delta epsilon zeta"])
            )
            out = CleanTranscriptWriter().write(
                source_video=fake_video,
                transcript=transcript,
                output_dir=root,
                clean_mode="raw",
            )
        assert out is not None
        self.assertEqual(out.name, "myclip.transcription.md")


class SummaryToMarkdownTest(unittest.TestCase):
    def test_wraps_llm_markdown_with_title(self) -> None:
        # The writer is intentionally dumb: whatever the LLM produced
        # ends up below the ``# Summary: <name>`` line verbatim.
        body = "## Огляд\nКоротке резюме відео.\n\n## Ключові факти\n- 60% часу"
        rendered = summary_to_markdown(video_name="myclip", summary_markdown=body)
        self.assertTrue(rendered.startswith("# Summary: myclip"))
        self.assertIn("## Огляд", rendered)
        self.assertIn("60% часу", rendered)

    def test_empty_body_renders_placeholder(self) -> None:
        # If callers ever pass an empty string explicitly (rather than
        # None, which short-circuits in the writer), we render a clear
        # "_No summary generated._" placeholder rather than an empty
        # file. The pipeline itself skips writing when the summary is
        # missing, so this branch is mostly defensive.
        rendered = summary_to_markdown(video_name="myclip", summary_markdown="")
        self.assertIn("No summary generated", rendered)


class SummaryWriterTest(unittest.TestCase):
    def test_skips_when_markdown_is_none(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_video = root / "myclip.mp4"
            fake_video.write_bytes(b"data")
            result = SummaryWriter().write(
                source_video=fake_video,
                summary_markdown=None,
                output_dir=root,
            )
        self.assertIsNone(result)

    def test_skips_when_markdown_is_whitespace(self) -> None:
        # Whitespace-only markdown means "nothing meaningful to write";
        # we drop the file rather than persist a near-empty stub.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_video = root / "myclip.mp4"
            fake_video.write_bytes(b"data")
            result = SummaryWriter().write(
                source_video=fake_video,
                summary_markdown="   \n  ",
                output_dir=root,
            )
        self.assertIsNone(result)

    def test_writes_expected_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_video = root / "myclip.mp4"
            fake_video.write_bytes(b"data")
            body = "## Огляд\nЦе огляд.\n\n## Дії та наміри\n- Зробити X."
            path = SummaryWriter().write(
                source_video=fake_video,
                summary_markdown=body,
                output_dir=root,
            )
            self.assertIsNotNone(path)
            assert path is not None
            self.assertEqual(path.name, "myclip.summary.md")
            content = path.read_text(encoding="utf-8-sig")
            self.assertIn("# Summary: myclip", content)
            self.assertIn("Це огляд.", content)
            self.assertIn("Зробити X.", content)


if __name__ == "__main__":
    unittest.main()
