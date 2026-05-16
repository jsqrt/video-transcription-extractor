"""Unit tests for the verbatim <stem>.raw.txt writer.

The raw file is the ground truth artefact. The test cases here lock down
its formatting so later changes don't silently break downstream consumers
(humans, and the cleanup module referencing the original Whisper output).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.types import Transcript, Utterance
from app.services.raw_writer import RawTranscriptWriter, transcript_to_raw_text


def _utt(text: str, *, start: float, end: float) -> Utterance:
    return Utterance(speaker="SPEAKER_1", text=text, start_sec=start, end_sec=end)


class TranscriptToRawTextTest(unittest.TestCase):
    def test_short_video_uses_mm_ss(self) -> None:
        transcript = Transcript(
            utterances=(
                _utt("перше речення", start=0.0, end=2.0),
                _utt("друге речення", start=61.0, end=63.0),
            )
        )
        text = transcript_to_raw_text(transcript)
        lines = text.splitlines()
        self.assertEqual(lines[0], "[00:00] перше речення")
        self.assertEqual(lines[1], "[01:01] друге речення")

    def test_long_video_uses_hh_mm_ss_everywhere(self) -> None:
        transcript = Transcript(
            utterances=(
                _utt("перше речення", start=0.0, end=2.0),
                _utt("останнє речення", start=3605.0, end=3608.0),
            )
        )
        text = transcript_to_raw_text(transcript)
        lines = text.splitlines()
        # Because the last utterance crosses 1h, every line aligns with HH:MM:SS.
        self.assertTrue(lines[0].startswith("[00:00:00]"))
        self.assertTrue(lines[1].startswith("[01:00:05]"))

    def test_no_chapter_headings_or_speaker_labels(self) -> None:
        transcript = Transcript(
            utterances=(
                _utt("ось що сказано", start=0.0, end=1.0),
            )
        )
        text = transcript_to_raw_text(transcript)
        self.assertNotIn("#", text)
        self.assertNotIn("[SPEAKER_1]", text)

    def test_empty_transcript_returns_empty_string(self) -> None:
        self.assertEqual(transcript_to_raw_text(Transcript(utterances=())), "")

    def test_blank_utterances_are_skipped(self) -> None:
        transcript = Transcript(
            utterances=(
                _utt("   ", start=0.0, end=1.0),
                _utt("справжнє речення", start=1.0, end=2.0),
            )
        )
        text = transcript_to_raw_text(transcript)
        self.assertEqual(text.splitlines(), ["[00:01] справжнє речення"])


class RawTranscriptWriterTest(unittest.TestCase):
    def test_writes_expected_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_video = root / "clip.mp4"
            fake_video.write_bytes(b"pretend")
            transcript = Transcript(
                utterances=(
                    _utt("перше речення", start=0.0, end=2.0),
                )
            )

            out = RawTranscriptWriter().write(
                source_video=fake_video,
                transcript=transcript,
                output_dir=root,
            )

            self.assertEqual(out.name, "clip.raw.txt")
            content = out.read_text(encoding="utf-8")
            self.assertEqual(content, "[00:00] перше речення\n")


if __name__ == "__main__":
    unittest.main()
