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

    def test_fast_profile_uses_small_beam(self) -> None:
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
        # pywhispercpp takes nested beam_search / greedy dicts, not flat
        # beam_size / best_of (those are faster-whisper's param names).
        self.assertEqual(fake_model.last_kwargs["beam_search"]["beam_size"], 2)
        self.assertEqual(fake_model.last_kwargs["greedy"]["best_of"], 2)
        self.assertEqual(fake_model.last_kwargs["language"], "en")

    def test_best_profile_uses_beam_five(self) -> None:
        # ``best`` uses beam search for accuracy — beam width matters a lot
        # for under-resourced languages like Ukrainian, where beam=5
        # catches morphology distinctions (e.g. "додавання" vs "додування")
        # that beam=3 occasionally misses on a quantized medium model.
        fake_model = _FakeModel([_FakeSegment(0.0, 10.0, "x")])
        _install_fake_pywhispercpp(lambda model: fake_model)

        provider = WhisperCppProvider()
        provider.transcribe(
            audio_path=self.audio_path,
            model=str(self.model_path),
            profile="best",
            language="en",
            timeout_sec=0,
        )
        self.assertEqual(fake_model.last_kwargs["beam_search"]["beam_size"], 5)
        self.assertEqual(fake_model.last_kwargs["greedy"]["best_of"], 5)
        # audio_ctx is unset by default → whisper.cpp uses full context (1500).
        self.assertNotIn("audio_ctx", fake_model.last_kwargs)

    def test_initial_prompt_passed_through(self) -> None:
        # The vocabulary hint must reach whisper.cpp verbatim. Without
        # it the decoder "translates" English loanwords ("пейджа") into
        # Ukrainian or fuses them with adjacent numerals ("пейдждва"),
        # so this knob is load-bearing on Ukrainian content.
        fake_model = _FakeModel([_FakeSegment(0.0, 10.0, "x")])
        _install_fake_pywhispercpp(lambda model: fake_model)

        provider = WhisperCppProvider()
        provider.transcribe(
            audio_path=self.audio_path,
            model=str(self.model_path),
            profile="best",
            language="uk",
            timeout_sec=0,
            initial_prompt="пейдж, фронтенд, бекенд",
        )
        self.assertEqual(
            fake_model.last_kwargs["initial_prompt"],
            "пейдж, фронтенд, бекенд",
        )

    def test_initial_prompt_absent_when_not_provided(self) -> None:
        # No prompt → key must be absent (not empty string), so
        # whisper.cpp uses its built-in no-prompt path.
        fake_model = _FakeModel([_FakeSegment(0.0, 10.0, "x")])
        _install_fake_pywhispercpp(lambda model: fake_model)

        provider = WhisperCppProvider()
        provider.transcribe(
            audio_path=self.audio_path,
            model=str(self.model_path),
            profile="best",
            language="uk",
            timeout_sec=0,
        )
        self.assertNotIn("initial_prompt", fake_model.last_kwargs)

    def test_initial_prompt_blank_is_dropped(self) -> None:
        # Whitespace-only prompt is equivalent to no prompt — must not
        # override whisper.cpp's default behaviour with an empty string.
        fake_model = _FakeModel([_FakeSegment(0.0, 10.0, "x")])
        _install_fake_pywhispercpp(lambda model: fake_model)

        provider = WhisperCppProvider()
        provider.transcribe(
            audio_path=self.audio_path,
            model=str(self.model_path),
            profile="best",
            language="uk",
            timeout_sec=0,
            initial_prompt="   \n  ",
        )
        self.assertNotIn("initial_prompt", fake_model.last_kwargs)

    def test_anti_hallucination_params_set(self) -> None:
        # The temperature fallback ladder (temperature_inc > 0) plus the
        # entropy / no-speech thresholds are what break repeat-loops and
        # suppress invented text over music. Guard them so a future speed
        # tweak doesn't silently disable them again.
        fake_model = _FakeModel([_FakeSegment(0.0, 10.0, "x")])
        _install_fake_pywhispercpp(lambda model: fake_model)

        provider = WhisperCppProvider()
        provider.transcribe(
            audio_path=self.audio_path,
            model=str(self.model_path),
            profile="best",
            language="en",
            timeout_sec=0,
        )
        kwargs = fake_model.last_kwargs
        self.assertTrue(kwargs["no_context"])
        self.assertGreater(kwargs["temperature_inc"], 0.0)
        self.assertEqual(kwargs["entropy_thold"], 2.4)
        self.assertEqual(kwargs["no_speech_thold"], 0.6)
        self.assertTrue(kwargs["suppress_blank"])

    def test_language_none_means_auto(self) -> None:
        # When the model doesn't expose auto_detect_language (older
        # pywhispercpp builds), the provider must still produce a
        # transcript by handing whisper.cpp's own per-segment auto path.
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
        # No detection happened, so nothing is exposed to the pipeline.
        self.assertIsNone(provider.last_detected_language)
        self.assertIsNone(provider.last_detected_language_probability)

    def test_auto_detected_language_surfaces_but_decoder_stays_auto(self) -> None:
        # Mixed-language mode: detection runs so the summarizer knows
        # the dominant language (text-only LLMs can't reliably tell
        # Ukrainian from Russian on Cyrillic), but the decoder is left
        # in "auto" so per-segment detection picks the right script for
        # each chunk. Pinning the decoder to a single language is what
        # made Whisper "translate" English loanwords ("пейдж" → some
        # phonetically-similar Ukrainian word), which is the failure
        # mode this test guards against.
        class _ModelWithDetection(_FakeModel):
            def auto_detect_language(self, audio_path, n_threads=4):
                return (("uk", 0.91), {"uk": 0.91, "ru": 0.05})

        fake_model = _ModelWithDetection([_FakeSegment(0.0, 10.0, "x")])
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
        self.assertEqual(provider.last_detected_language, "uk")
        self.assertAlmostEqual(
            provider.last_detected_language_probability, 0.91, places=4
        )

    def test_auto_detect_failure_falls_back_to_auto(self) -> None:
        # If the detection call raises (model corrupted, signature
        # changed in a future pywhispercpp, etc.), the provider must
        # NOT abort the pipeline — it must keep transcribing with
        # whisper.cpp's per-segment auto path and leave the detected
        # language unset.
        class _BrokenDetection(_FakeModel):
            def auto_detect_language(self, audio_path, n_threads=4):
                raise RuntimeError("simulated detection crash")

        fake_model = _BrokenDetection([_FakeSegment(0.0, 10.0, "x")])
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
        self.assertIsNone(provider.last_detected_language)

    def test_forced_language_skips_detection(self) -> None:
        # When the user pins a language explicitly, the provider must
        # trust it and not call auto_detect_language — the user override
        # wins, and ``last_detected_language`` stays empty so the
        # pipeline can route the forced value through unchanged.
        class _TrackingDetection(_FakeModel):
            def __init__(self, segments):
                super().__init__(segments)
                self.detect_calls = 0

            def auto_detect_language(self, audio_path, n_threads=4):
                self.detect_calls += 1
                return (("uk", 0.91), {})

        fake_model = _TrackingDetection([_FakeSegment(0.0, 10.0, "x")])
        _install_fake_pywhispercpp(lambda model: fake_model)

        provider = WhisperCppProvider()
        provider.transcribe(
            audio_path=self.audio_path,
            model=str(self.model_path),
            profile="best",
            language="en",
            timeout_sec=0,
        )
        self.assertEqual(fake_model.last_kwargs["language"], "en")
        self.assertEqual(fake_model.detect_calls, 0)
        self.assertIsNone(provider.last_detected_language)


class DropUnsupportedParamsTest(unittest.TestCase):
    """A version skew between pywhispercpp builds means some flat params
    (e.g. ``suppress_non_speech_tokens`` → ``suppress_nst``) may be absent
    from the compiled params object. The provider filters those out rather
    than letting setattr raise AttributeError mid-transcription."""

    class _Params:
        # Mimics a real whisper_full_params: only these attributes exist.
        no_context = True
        temperature = 0.0
        suppress_blank = True

    class _ModelWithParams:
        def __init__(self) -> None:
            self._params = DropUnsupportedParamsTest._Params()

    def test_unknown_flat_param_is_dropped(self) -> None:
        model = self._ModelWithParams()
        params = {
            "no_context": True,
            "suppress_blank": True,
            "suppress_non_speech_tokens": True,  # absent on this build
            "beam_search": {"beam_size": 5},  # nested → always kept
            "greedy": {"best_of": 5},
        }
        kept = WhisperCppProvider._drop_unsupported_params(model, params)
        self.assertNotIn("suppress_non_speech_tokens", kept)
        self.assertIn("no_context", kept)
        self.assertIn("suppress_blank", kept)
        # Nested decoder configs survive even though _Params has no such attr.
        self.assertIn("beam_search", kept)
        self.assertIn("greedy", kept)

    def test_model_without_params_attr_is_unfiltered(self) -> None:
        # A test double (or future backend) without ``_params`` must not
        # have its params silently stripped.
        params = {"no_context": True, "anything": 1}
        kept = WhisperCppProvider._drop_unsupported_params(object(), params)
        self.assertEqual(kept, params)


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
