"""Unit tests for the shared pipeline wrapper used by the CLI and the
MCP server."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.types import (
    SummaryOptions,
    TranscribeOptions,
    Transcript,
    Utterance,
)
from app.services.pipeline import PipelineResult, run_pipeline
from app.services.summarizer import Summarizer


class _FakeExtractor:
    def __init__(self) -> None:
        self.called: list[tuple[Path, Path]] = []

    def extract(self, *, video_path: Path, output_wav_path: Path) -> None:
        self.called.append((video_path, output_wav_path))
        output_wav_path.write_bytes(b"RIFF")


class _FakeTranscriber:
    def __init__(self, transcript: Transcript) -> None:
        self.transcript = transcript
        self.calls: list[dict] = []

    def transcribe(self, **kwargs) -> Transcript:
        self.calls.append(kwargs)
        return self.transcript


def _make_transcript(sentences: list[str]) -> Transcript:
    return Transcript(
        utterances=tuple(
            Utterance(
                speaker="SPEAKER_1",
                text=text,
                start_sec=i * 2.0,
                end_sec=i * 2.0 + 2.0,
            )
            for i, text in enumerate(sentences)
        )
    )


class RunPipelineTest(unittest.TestCase):
    def test_writes_both_files_and_returns_stats(self) -> None:
        transcript = _make_transcript(
            [
                "Іран контролює Ормузьку протоку.",
                "Нафта знову дорожчає до 100 доларів.",
                "Трамп оголосив нові умови.",
                "Ринок реагує швидко.",
                "Китай спостерігає за ситуацією.",
                "Турція розглядає підвищення мита.",
                "Аналітики бачать новий світ.",
                "Європа має реагувати.",
            ]
            * 3
        )
        extractor = _FakeExtractor()
        transcriber = _FakeTranscriber(transcript)
        summarizer = Summarizer(
            options=SummaryOptions(
                mode="extractive",
                per_chapter_sentences=2,
                overview_sentences=3,
            )
        )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_path = root / "myclip.mp4"
            video_path.write_bytes(b"pretend")
            out_dir = root / "out"

            result = run_pipeline(
                video_path=video_path,
                options=TranscribeOptions(
                    summary=SummaryOptions(mode="extractive"),
                ),
                output_dir=out_dir,
                extractor=extractor,
                transcriber=transcriber,
                summarizer=summarizer,
            )

            self.assertIsInstance(result, PipelineResult)
            self.assertGreaterEqual(result.chapter_count, 1)
            self.assertEqual(result.utterance_count, len(transcript.utterances))

            # Duration is taken from the last utterance's end timestamp.
            self.assertAlmostEqual(
                result.duration_seconds,
                transcript.utterances[-1].end_sec or 0.0,
                places=3,
            )

            # clean.md exists.
            self.assertIsNotNone(result.transcript_path)
            assert result.transcript_path is not None
            self.assertTrue(result.transcript_path.exists())
            self.assertEqual(result.transcript_path.name, "myclip.transcription.md")

            # summary.md exists.
            self.assertIsNotNone(result.summary_path)
            assert result.summary_path is not None
            self.assertTrue(result.summary_path.exists())
            self.assertEqual(result.summary_path.name, "myclip.summary.md")

        # The extractor must be asked to write the WAV into a temp dir.
        self.assertEqual(len(extractor.called), 1)
        video, wav = extractor.called[0]
        self.assertEqual(video, video_path)
        self.assertTrue(wav.name.endswith(".wav"))

    def test_pipeline_result_has_no_raw_field(self) -> None:
        """Guard: raw transcript output is intentionally removed."""
        self.assertNotIn(
            "raw_transcript_path",
            {f.name for f in PipelineResult.__dataclass_fields__.values()},
        )

    def test_skips_summary_file_when_disabled(self) -> None:
        transcript = _make_transcript(
            [
                "щось цікаве про нафту",
                "ще щось про ринок",
                "третя фраза про війну",
            ]
            * 6
        )
        extractor = _FakeExtractor()
        transcriber = _FakeTranscriber(transcript)
        summarizer = Summarizer(options=SummaryOptions(mode="extractive"))

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_path = root / "clip.mp4"
            video_path.write_bytes(b"pretend")

            result = run_pipeline(
                video_path=video_path,
                options=TranscribeOptions(
                    summary=SummaryOptions(mode="extractive"),
                ),
                output_dir=root,
                write_summary_file=False,
                extractor=extractor,
                transcriber=transcriber,
                summarizer=summarizer,
            )
            self.assertIsNotNone(result.transcript_path)
            assert result.transcript_path is not None
            self.assertTrue(result.transcript_path.exists())
            self.assertIsNone(result.summary_path)
            self.assertFalse((root / "clip.summary.md").exists())

    def test_skips_clean_file_when_disabled(self) -> None:
        transcript = _make_transcript(["alpha", "beta", "gamma"])
        extractor = _FakeExtractor()
        transcriber = _FakeTranscriber(transcript)
        summarizer = Summarizer(options=SummaryOptions(mode="none"))

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_path = root / "clip.mp4"
            video_path.write_bytes(b"pretend")

            result = run_pipeline(
                video_path=video_path,
                options=TranscribeOptions(summary=SummaryOptions(mode="none")),
                output_dir=root,
                write_clean_file=False,
                extractor=extractor,
                transcriber=transcriber,
                summarizer=summarizer,
            )
        self.assertIsNone(result.transcript_path)
        self.assertFalse((root / "clip.transcription.md").exists())

    def test_summary_mode_none_produces_no_summary(self) -> None:
        transcript = _make_transcript(
            [
                "one two three four",
                "five six seven eight",
                "nine ten eleven twelve",
            ]
            * 6
        )
        extractor = _FakeExtractor()
        transcriber = _FakeTranscriber(transcript)
        summarizer = Summarizer(options=SummaryOptions(mode="none"))

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_path = root / "clip.mp4"
            video_path.write_bytes(b"pretend")

            result = run_pipeline(
                video_path=video_path,
                options=TranscribeOptions(summary=SummaryOptions(mode="none")),
                output_dir=root,
                extractor=extractor,
                transcriber=transcriber,
                summarizer=summarizer,
            )
        self.assertIsNone(result.summary_path)

    def test_rule_based_mode_drops_duplicates_from_clean_file(self) -> None:
        transcript = Transcript(
            utterances=(
                Utterance(
                    speaker="SPEAKER_1",
                    text="Це важливий факт.",
                    start_sec=0.0,
                    end_sec=2.0,
                ),
                Utterance(
                    speaker="SPEAKER_1",
                    text="Це важливий факт.",
                    start_sec=2.1,
                    end_sec=4.0,
                ),
                Utterance(
                    speaker="SPEAKER_1",
                    text="А тепер щось інше.",
                    start_sec=4.1,
                    end_sec=6.0,
                ),
            )
        )
        extractor = _FakeExtractor()
        transcriber = _FakeTranscriber(transcript)
        summarizer = Summarizer(options=SummaryOptions(mode="none"))

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_path = root / "clip.mp4"
            video_path.write_bytes(b"pretend")

            result = run_pipeline(
                video_path=video_path,
                options=TranscribeOptions(
                    summary=SummaryOptions(mode="none"),
                    include_chapters=False,
                ),
                output_dir=root,
                clean_mode="rule-based",
                extractor=extractor,
                transcriber=transcriber,
                summarizer=summarizer,
            )

            assert result.transcript_path is not None
            clean_text = result.transcript_path.read_text(encoding="utf-8")

        self.assertEqual(clean_text.count("Це важливий факт."), 1)

    def test_runs_without_optional_services(self) -> None:
        """CleanTranscriptWriter / SummaryWriter have sane defaults."""
        transcript = _make_transcript(["alpha beta", "gamma delta"])
        extractor = _FakeExtractor()
        transcriber = _FakeTranscriber(transcript)
        summarizer = Summarizer(options=SummaryOptions(mode="none"))

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_path = root / "tiny.mp4"
            video_path.write_bytes(b"x")
            result = run_pipeline(
                video_path=video_path,
                options=TranscribeOptions(summary=SummaryOptions(mode="none")),
                output_dir=root,
                extractor=extractor,
                transcriber=transcriber,
                summarizer=summarizer,
            )
            assert result.transcript_path is not None
            self.assertTrue(result.transcript_path.exists())


if __name__ == "__main__":
    unittest.main()
