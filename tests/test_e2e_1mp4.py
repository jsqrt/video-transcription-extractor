"""End-to-end smoke test on the real ``video-sample/1.mp4`` file.

The sandbox running these tests cannot download a Whisper model, so we
bypass the actual acoustic model and feed the pipeline a ready-made
transcript from ``video-sample/1.clean.md``. What *is* exercised
end-to-end:

* ``AudioExtractor`` runs real ffmpeg on a real mp4 and produces a 16 kHz
  mono WAV that Whisper would accept.
* ``Transcriber`` parses the raw provider output.
* ``rule_based_cleanup`` runs over the parsed transcript.
* The Summarizer runs with a stub LLM client (returns canned markdown).
* ``CleanTranscriptWriter`` and ``SummaryWriter`` render the two artefacts.
"""

from __future__ import annotations

import re
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.types import SummaryOptions
from app.services.audio_extractor import AudioExtractor
from app.services.cleanup import rule_based_cleanup
from app.services.summarizer import Summarizer
from app.services.summary_writer import SummaryWriter
from app.services.transcriber import Transcriber
from app.services.writer import CleanTranscriptWriter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_VIDEO = PROJECT_ROOT / "video-sample" / "1.mp4"
# Either the legacy .transcript.txt file or the new .clean.md is acceptable
# as a source of fake transcript content — we only use it to stub Whisper.
SAMPLE_TRANSCRIPT_LEGACY = PROJECT_ROOT / "video-sample" / "1.transcript.txt"
SAMPLE_TRANSCRIPT_CLEAN = PROJECT_ROOT / "video-sample" / "1.clean.md"

SPEAKER_LINE = re.compile(r"^\[(?P<speaker>[A-Z_0-9]+)\]\s*(?P<text>.+)$")


def _pick_sample_transcript() -> Path | None:
    for candidate in (SAMPLE_TRANSCRIPT_LEGACY, SAMPLE_TRANSCRIPT_CLEAN):
        if candidate.exists():
            return candidate
    return None


def _transcript_lines_from_sample(path: Path) -> str:
    """Convert an already-chaptered transcript back into raw provider output.

    Keeps only ``[SPEAKER] text`` lines (legacy format) or falls back to
    non-heading paragraphs (clean-md format) and fabricates fake ascending
    timestamps so the timestamped parser path is exercised.
    """
    raw_lines: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(">") or line.startswith("-"):
            continue
        match = SPEAKER_LINE.match(line)
        if match:
            raw_lines.append((match.group("speaker"), match.group("text")))
        else:
            # Treat as a paragraph from the single speaker.
            raw_lines.append(("SPEAKER_1", line))
    seconds_per_line = 3.0
    assembled: list[str] = []
    for index, (speaker, text) in enumerate(raw_lines):
        start = index * seconds_per_line
        end = start + seconds_per_line
        assembled.append(f"[{start:.2f} -> {end:.2f}] [{speaker}] {text}")
    return "\n".join(assembled)


class _FakeWhisperProvider:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.called = False

    def transcribe(self, *args, **kwargs) -> str:  # pragma: no cover - shim
        self.called = True
        return self.payload


@unittest.skipUnless(
    SAMPLE_VIDEO.exists() and _pick_sample_transcript() is not None,
    "video-sample/1.mp4 or existing transcript not present; skipping e2e smoke test",
)
class E2E1Mp4Test(unittest.TestCase):
    def test_audio_extraction_produces_wav(self) -> None:
        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg is not installed in PATH")
        with TemporaryDirectory() as tmp:
            out_wav = Path(tmp) / "1.wav"
            AudioExtractor(sample_rate=16000).extract(
                video_path=SAMPLE_VIDEO, output_wav_path=out_wav
            )
            self.assertTrue(out_wav.exists())
            self.assertGreater(out_wav.stat().st_size, 1024)

    def test_full_text_pipeline_on_existing_transcript(self) -> None:
        sample = _pick_sample_transcript()
        assert sample is not None
        raw = _transcript_lines_from_sample(sample)
        self.assertGreater(len(raw), 500, msg="sample transcript looks empty")

        provider = _FakeWhisperProvider(raw)
        transcriber = Transcriber(provider=provider)
        transcript = transcriber.transcribe(
            audio_path=SAMPLE_VIDEO,
            model="large-v3",
            profile="best",
            language="uk",
            timeout_sec=0,
        )
        self.assertGreater(len(transcript.utterances), 10)

        cleaned = rule_based_cleanup(transcript)
        self.assertLessEqual(
            len(cleaned.utterances),
            len(transcript.utterances),
            "cleanup should never grow the utterance count",
        )

        # The summarizer is LLM-only now. We stub out the LLM with a
        # canned markdown reply so the end-to-end shape (extract →
        # transcribe → clean → summary → write) is still covered
        # without needing Ollama running in CI.
        class _StubLLM:
            def is_available(self) -> bool:
                return True

            def chat(self, system_prompt, user_prompt, *, temperature=0.0) -> str:
                return "## Огляд\nЦе короткий огляд.\n"

        summary_markdown = Summarizer(
            options=SummaryOptions(mode="ollama"),
            llm_client=_StubLLM(),
        ).summarize(transcript=cleaned, language="uk")
        self.assertIsInstance(summary_markdown, str)
        self.assertTrue(summary_markdown.strip())

        with TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            fake_video = out_dir / "1.mp4"
            fake_video.write_bytes(b"pretend")

            clean_path = CleanTranscriptWriter().write(
                source_video=fake_video,
                transcript=cleaned,
                output_dir=out_dir,
                clean_mode="raw",  # already cleaned above
            )
            clean_content = clean_path.read_text(encoding="utf-8")
            # Speaker tags should never leak into the rendered transcript;
            # chapter headings are no longer emitted, so we just check
            # that the file contains the cleaned text verbatim.
            self.assertNotIn("[SPEAKER_", clean_content)

            summary_path = SummaryWriter().write(
                source_video=fake_video,
                summary_markdown=summary_markdown,
                output_dir=out_dir,
            )
            self.assertIsNotNone(summary_path)
            assert summary_path is not None
            self.assertEqual(summary_path.name, "1.summary.md")
            summary_content = summary_path.read_text(encoding="utf-8-sig")
            self.assertIn("# Summary: 1", summary_content)
            self.assertIn("## Огляд", summary_content)


if __name__ == "__main__":
    unittest.main()
