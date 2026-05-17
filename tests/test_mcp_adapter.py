"""Smoke tests for the MCP server's PipelineAdapter.

These tests do NOT require the ``mcp`` SDK (the adapter deliberately sits
below the SDK layer). They also do NOT run Whisper — a fake pipeline is
injected so the shape of the response can be verified deterministically.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from app.models.types import TranscribeOptions
from app.services.pipeline import PipelineResult
from mcp_server.adapter import (
    AdapterError,
    PipelineAdapter,
    TranscribeArguments,
    TranscribeResponse,
)


@dataclass
class _Recorded:
    calls: list[dict]


def _fake_pipeline_factory(
    recorded: _Recorded,
    *,
    clean_name: str = "fake.clean.md",
    summary_name: Optional[str] = "fake.summary.md",
    duration: float = 123.456,
    chapter_count: int = 4,
    utterance_count: int = 42,
):
    def _fake_run(**kwargs) -> PipelineResult:
        recorded.calls.append(kwargs)
        output_dir: Path = kwargs["output_dir"] or Path(kwargs["video_path"]).parent
        output_dir.mkdir(parents=True, exist_ok=True)

        transcript_path: Optional[Path] = None
        if kwargs.get("write_clean_file", True):
            transcript_path = output_dir / clean_name
            transcript_path.write_text("fake clean", encoding="utf-8")

        summary_path: Optional[Path] = None
        if summary_name and kwargs.get("write_summary_file", True):
            summary_path = output_dir / summary_name
            summary_path.write_text("fake summary", encoding="utf-8")

        return PipelineResult(
            transcript_path=transcript_path,
            summary_path=summary_path,
            duration_seconds=duration,
            chapter_count=chapter_count,
            utterance_count=utterance_count,
        )

    return _fake_run


def _dummy_factory_chain():
    """Create no-op factory callables so PipelineAdapter doesn't try to
    construct real providers (which would import faster-whisper)."""
    return {
        "extractor_factory": lambda: object(),
        "transcriber_factory": lambda: object(),
        "summarizer_factory": lambda options: object(),
        "clean_writer_factory": lambda mode: object(),
        "summary_writer_factory": lambda: object(),
    }


class ValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recorded = _Recorded(calls=[])
        self.adapter = PipelineAdapter(
            pipeline_fn=_fake_pipeline_factory(self.recorded),
            **_dummy_factory_chain(),
        )

    def test_requires_absolute_path(self) -> None:
        with self.assertRaises(AdapterError) as ctx:
            self.adapter.transcribe(TranscribeArguments(file_path="relative.mp4"))
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_rejects_missing_file(self) -> None:
        # Construct an absolute path that is valid on this platform but
        # cannot exist (a long random tail under the system temp root).
        missing = Path(TemporaryDirectory().name) / "no_such_dir" / "missing.mp4"
        self.assertTrue(missing.is_absolute())
        args = TranscribeArguments(file_path=str(missing))
        with self.assertRaises(AdapterError) as ctx:
            self.adapter.transcribe(args)
        self.assertEqual(ctx.exception.code, "not_found")

    def test_rejects_unsupported_extension(self) -> None:
        with TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "readme.txt"
            bogus.write_text("no")
            with self.assertRaises(AdapterError) as ctx:
                self.adapter.transcribe(
                    TranscribeArguments(file_path=str(bogus))
                )
        self.assertEqual(ctx.exception.code, "unsupported_format")

    def test_accepts_audio_extension(self) -> None:
        with TemporaryDirectory() as tmp:
            media = Path(tmp) / "clip.mp3"
            media.write_bytes(b"\x00" * 16)
            response = self.adapter.transcribe(
                TranscribeArguments(file_path=str(media))
            )
        self.assertIsInstance(response, TranscribeResponse)

    def test_rejects_unknown_summary_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            media = Path(tmp) / "clip.mp4"
            media.write_bytes(b"\x00")
            with self.assertRaises(AdapterError) as ctx:
                self.adapter.transcribe(
                    TranscribeArguments(
                        file_path=str(media),
                        summary_mode="gpt4o",  # type: ignore[arg-type]
                    )
                )
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_rejects_unknown_clean_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            media = Path(tmp) / "clip.mp4"
            media.write_bytes(b"\x00")
            with self.assertRaises(AdapterError) as ctx:
                self.adapter.transcribe(
                    TranscribeArguments(
                        file_path=str(media),
                        clean_mode="bogus",  # type: ignore[arg-type]
                    )
                )
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_rejects_relative_output_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            media = Path(tmp) / "clip.mp4"
            media.write_bytes(b"\x00")
            with self.assertRaises(AdapterError) as ctx:
                self.adapter.transcribe(
                    TranscribeArguments(
                        file_path=str(media),
                        output_dir="relative/path",
                    )
                )
        self.assertEqual(ctx.exception.code, "invalid_argument")


class ShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recorded = _Recorded(calls=[])
        self.adapter = PipelineAdapter(
            pipeline_fn=_fake_pipeline_factory(
                self.recorded,
                clean_name="video.clean.md",
                summary_name="video.summary.md",
                duration=17.25,
                chapter_count=3,
                utterance_count=88,
            ),
            **_dummy_factory_chain(),
        )

    def test_happy_path_response_shape(self) -> None:
        with TemporaryDirectory() as tmp:
            media = Path(tmp) / "video.mp4"
            media.write_bytes(b"\x00" * 32)
            out_dir = Path(tmp) / "out"
            response = self.adapter.transcribe(
                TranscribeArguments(
                    file_path=str(media),
                    output_dir=str(out_dir),
                    summary_mode="extractive",
                    language="uk",
                    profile="fast",
                )
            )

            self.assertIsInstance(response, TranscribeResponse)
            self.assertEqual(response.duration_seconds, 17.25)
            self.assertEqual(response.chapter_count, 3)
            self.assertEqual(response.utterance_count, 88)
            assert response.transcript_path is not None
            self.assertTrue(response.transcript_path.endswith("video.clean.md"))
            assert response.summary_path is not None
            self.assertTrue(response.summary_path.endswith("video.summary.md"))

            as_dict = response.as_dict()
            self.assertEqual(
                set(as_dict.keys()),
                {
                    "transcript_path",
                    "summary_path",
                    "duration_seconds",
                    "chapter_count",
                    "utterance_count",
                    "trail_loop_dropped",
                },
            )
            self.assertEqual(as_dict["trail_loop_dropped"], 0)

    def test_summary_mode_none_skips_summary_file(self) -> None:
        self.adapter = PipelineAdapter(
            pipeline_fn=_fake_pipeline_factory(
                self.recorded,
                summary_name=None,
            ),
            **_dummy_factory_chain(),
        )
        with TemporaryDirectory() as tmp:
            media = Path(tmp) / "clip.wav"
            media.write_bytes(b"\x00" * 16)
            response = self.adapter.transcribe(
                TranscribeArguments(
                    file_path=str(media),
                    summary_mode="none",
                )
            )
        self.assertIsNone(response.summary_path)
        # Verify the adapter forwarded write_summary_file=False.
        self.assertEqual(self.recorded.calls[-1]["write_summary_file"], False)

    def test_write_clean_false_skips_clean_file(self) -> None:
        with TemporaryDirectory() as tmp:
            media = Path(tmp) / "clip.mp4"
            media.write_bytes(b"\x00" * 16)
            response = self.adapter.transcribe(
                TranscribeArguments(
                    file_path=str(media),
                    write_clean=False,
                )
            )
        self.assertIsNone(response.transcript_path)
        self.assertEqual(self.recorded.calls[-1]["write_clean_file"], False)

    def test_pipeline_receives_correct_options(self) -> None:
        with TemporaryDirectory() as tmp:
            media = Path(tmp) / "clip.mp4"
            media.write_bytes(b"\x00" * 16)
            self.adapter.transcribe(
                TranscribeArguments(
                    file_path=str(media),
                    summary_mode="ollama",
                    chapters=False,
                    language="uk",
                    profile="best",
                    model="small",
                    title_style="snippet",
                    timeout_sec=300,
                    clean_mode="llm",
                )
            )
        call = self.recorded.calls[-1]
        options = call["options"]
        self.assertIsInstance(options, TranscribeOptions)
        self.assertEqual(options.profile, "best")
        self.assertEqual(options.model, "small")
        self.assertEqual(options.language, "uk")
        self.assertEqual(options.timeout_sec, 300)
        self.assertFalse(options.include_chapters)
        self.assertEqual(options.summary.mode, "ollama")
        self.assertEqual(call["title_style"], "snippet")
        self.assertEqual(call["clean_mode"], "llm")


if __name__ == "__main__":
    unittest.main()
