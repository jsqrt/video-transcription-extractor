"""Unit tests for WhisperCppProvider.

We mock ``pywhispercpp.model.Model`` because the real library is only
installed on macOS shipping hosts. The tests exercise the protocol shape
(line format produced for the Transcriber wrapper) and the failure
modes (missing model file, import failure, empty output, cancellation).
"""

from __future__ import annotations

import sys
import threading
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.models.types import (
    ModelNotFoundError,
    TranscriptionError,
    TranscriptionTimeoutError,
)
from app.providers.whisper_cpp_provider import WhisperCppProvider


class _FakeSegment:
    def __init__(self, t0: float, t1: float, text: str) -> None:
        self.t0 = t0
        self.t1 = t1
        self.text = text


class _FakeModel:
    def __init__(self, segments: list[_FakeSegment]) -> None:
        self._segments = segments
        self.last_kwargs: dict | None = None

    def transcribe(self, audio_path: str, **kwargs) -> list[_FakeSegment]:
        self.last_kwargs = kwargs
        return self._segments


def _install_fake_pywhispercpp(model_factory) -> None:
    """Inject a fake ``pywhispercpp.model.Model`` into sys.modules."""
    fake_module = types.ModuleType("pywhispercpp.model")
    fake_module.Model = model_factory  # type: ignore[attr-defined]
    fake_root = types.ModuleType("pywhispercpp")
    fake_root.model = fake_module  # type: ignore[attr-defined]
    sys.modules["pywhispercpp"] = fake_root
    sys.modules["pywhispercpp.model"] = fake_module


def _uninstall_fake_pywhispercpp() -> None:
    sys.modules.pop("pywhispercpp.model", None)
    sys.modules.pop("pywhispercpp", None)


class TranscribeOutputShapeTest(unittest.TestCase):
    """The transcriber wrapper parses lines like
    ``[start -> end] [SPEAKER] text``. The provider must produce
    exactly that format so the regex in transcriber.py matches."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.model_path = Path(self._tmp.name) / "ggml-large-v3.bin"
        self.model_path.write_bytes(b"fake-ggml")
        self.audio_path = Path(self._tmp.name) / "audio.wav"
        self.audio_path.write_bytes(b"RIFF")

    def tearDown(self) -> None:
        _uninstall_fake_pywhispercpp()
        self._tmp.cleanup()

    def test_segments_translate_to_speaker_lines(self) -> None:
        fake_model = _FakeModel([
            _FakeSegment(0.0, 150.0, "hello world"),
            _FakeSegment(150.0, 320.0, "second utterance"),
        ])
        _install_fake_pywhispercpp(lambda model: fake_model)

        provider = WhisperCppProvider()
        text = provider.transcribe(
            audio_path=self.audio_path,
            model=str(self.model_path),
            profile="best",
            language=None,
            timeout_sec=0,
        )
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            lines[0],
            "[0.00 -> 1.50] [UNKNOWN_SPEAKER] hello world",
        )
        self.assertEqual(
            lines[1],
            "[1.50 -> 3.20] [UNKNOWN_SPEAKER] second utterance",
        )

    def test_empty_segments_raise(self) -> None:
        _install_fake_pywhispercpp(lambda model: _FakeModel([]))
        provider = WhisperCppProvider()
        with self.assertRaises(TranscriptionError):
            provider.transcribe(
                audio_path=self.audio_path,
                model=str(self.model_path),
                profile="best",
                language=None,
                timeout_sec=0,
            )

    def test_fast_profile_uses_smaller_beam(self) -> None:
        fake_model = _FakeModel([_FakeSegment(0.0, 10.0, "x")])
        _install_fake_pywhispercpp(lambda model: fake_model)

        provider = WhisperCppProvider()
        provider.transcribe(
            audio_path=self.audio_path,
            model=str(self.model_path),
            profile="fast",
            language="en",
            timeout_sec=0,
        )
        self.assertEqual(fake_model.last_kwargs["beam_size"], 2)
        self.assertEqual(fake_model.last_kwargs["language"], "en")

    def test_language_none_means_auto(self) -> None:
        fake_model = _FakeModel([_FakeSegment(0.0, 10.0, "x")])
        _install_fake_pywhispercpp(lambda model: fake_model)

        provider = WhisperCppProvider()
        provider.transcribe(
            audio_path=self.audio_path,
            model=str(self.model_path),
            profile="best",
            language=None,
            timeout_sec=0,
        )
        self.assertEqual(fake_model.last_kwargs["language"], "auto")


class FailureModeTest(unittest.TestCase):
    def test_missing_model_file_raises(self) -> None:
        # Even if pywhispercpp would import, a missing model path is a
        # hard error.
        _install_fake_pywhispercpp(lambda model: _FakeModel([]))
        provider = WhisperCppProvider()
        with self.assertRaises(ModelNotFoundError):
            provider.transcribe(
                audio_path=Path("audio.wav"),
                model="/no/such/file.bin",
                profile="best",
                language=None,
                timeout_sec=0,
            )

    def test_pywhispercpp_missing_raises_transcription_error(self) -> None:
        # Wipe any cached import to simulate a CPython runtime without
        # the wheel installed.
        with patch.dict(sys.modules):
            sys.modules.pop("pywhispercpp.model", None)
            sys.modules.pop("pywhispercpp", None)
            # And block re-importing.
            with patch.object(
                sys, "meta_path", []
            ):
                with TemporaryDirectory() as tmp:
                    mp = Path(tmp) / "x.bin"
                    mp.write_bytes(b"x")
                    provider = WhisperCppProvider()
                    with self.assertRaises(TranscriptionError):
                        provider.transcribe(
                            audio_path=Path(tmp) / "audio.wav",
                            model=str(mp),
                            profile="best",
                            language=None,
                            timeout_sec=0,
                        )


class CancellationTest(unittest.TestCase):
    def test_cancel_event_set_before_transcribe_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "g.bin"
            model_path.write_bytes(b"x")
            _install_fake_pywhispercpp(
                lambda model: _FakeModel([_FakeSegment(0.0, 10.0, "x")])
            )
            event = threading.Event()
            event.set()
            provider = WhisperCppProvider()
            with self.assertRaises(TranscriptionTimeoutError):
                provider.transcribe(
                    audio_path=Path(tmp) / "audio.wav",
                    model=str(model_path),
                    profile="best",
                    language=None,
                    timeout_sec=0,
                    cancel_event=event,
                )
        _uninstall_fake_pywhispercpp()


if __name__ == "__main__":
    unittest.main()
