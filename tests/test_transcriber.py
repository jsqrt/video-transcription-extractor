from __future__ import annotations

import unittest

from app.models.types import TranscriptionError
from app.services.transcriber import Transcriber


class _FakeProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    def transcribe(self, *args, **kwargs) -> str:  # pragma: no cover - shim
        return self._text


class TranscriberParseTest(unittest.TestCase):
    def test_timestamped_line(self) -> None:
        transcriber = Transcriber(provider=_FakeProvider(
            "[0.10 -> 2.40] [SPEAKER_1] hello world\n"
            "[2.50 -> 4.00] [UNKNOWN_SPEAKER] the answer is 42"
        ))
        transcript = transcriber.transcribe(
            audio_path=None, model="x", profile="best", language=None, timeout_sec=0
        )
        self.assertEqual(len(transcript.utterances), 2)
        self.assertEqual(transcript.utterances[0].speaker, "SPEAKER_1")
        self.assertAlmostEqual(transcript.utterances[0].start_sec, 0.10)
        self.assertAlmostEqual(transcript.utterances[0].end_sec, 2.40)
        self.assertEqual(transcript.utterances[1].text, "the answer is 42")

    def test_swapped_end_before_start_is_corrected(self) -> None:
        transcriber = Transcriber(provider=_FakeProvider(
            "[5.00 -> 3.00] [SPEAKER_1] weird ordering"
        ))
        transcript = transcriber.transcribe(
            audio_path=None, model="x", profile="best", language=None, timeout_sec=0
        )
        self.assertAlmostEqual(transcript.utterances[0].start_sec, 3.00)
        self.assertAlmostEqual(transcript.utterances[0].end_sec, 5.00)

    def test_speaker_only_line(self) -> None:
        transcriber = Transcriber(provider=_FakeProvider(
            "[SPEAKER_2] no timestamps here"
        ))
        transcript = transcriber.transcribe(
            audio_path=None, model="x", profile="best", language=None, timeout_sec=0
        )
        self.assertEqual(transcript.utterances[0].speaker, "SPEAKER_2")
        self.assertIsNone(transcript.utterances[0].start_sec)

    def test_plain_line_becomes_unknown_speaker(self) -> None:
        transcriber = Transcriber(provider=_FakeProvider("just some raw text"))
        transcript = transcriber.transcribe(
            audio_path=None, model="x", profile="best", language=None, timeout_sec=0
        )
        self.assertEqual(transcript.utterances[0].speaker, "UNKNOWN_SPEAKER")

    def test_empty_input_raises(self) -> None:
        transcriber = Transcriber(provider=_FakeProvider(""))
        with self.assertRaises(TranscriptionError):
            transcriber.transcribe(
                audio_path=None, model="x", profile="best", language=None, timeout_sec=0
            )

    def test_trailing_repeat_loop_is_trimmed(self) -> None:
        # 30 copies of the same sentence at the end — typical Whisper loop hallucination.
        sentences = ["[0.0 -> 1.0] [SPEAKER_1] real useful statement number one",
                     "[1.0 -> 2.0] [SPEAKER_1] another real useful statement two",
                     "[2.0 -> 3.0] [SPEAKER_1] three real and useful"]
        repeat = "[10.0 -> 11.0] [SPEAKER_1] subscribe to our channel for more"
        text = "\n".join(sentences + [repeat] * 35)
        transcriber = Transcriber(provider=_FakeProvider(text))
        transcript = transcriber.transcribe(
            audio_path=None, model="x", profile="best", language=None, timeout_sec=0
        )
        # Repeats must be trimmed, originals must survive.
        texts = [u.text for u in transcript.utterances]
        self.assertLessEqual(texts.count("subscribe to our channel for more"), 2)
        self.assertIn("real useful statement number one", texts)

    def test_trail_loop_warning_and_counter_are_exposed(self) -> None:
        sentences = ["[0.0 -> 1.0] [SPEAKER_1] opening statement"]
        repeat = "[10.0 -> 11.0] [SPEAKER_1] here is the problem"
        text = "\n".join(sentences + [repeat] * 50)

        messages: list[str] = []
        transcriber = Transcriber(
            provider=_FakeProvider(text),
            logger_fn=messages.append,
        )
        transcriber.transcribe(
            audio_path=None, model="x", profile="best", language=None, timeout_sec=0
        )
        self.assertGreater(transcriber.last_trail_loop_trim, 30)
        self.assertIn("here is the problem", transcriber.last_trail_loop_sample)
        self.assertTrue(messages, "expected at least one warning emitted")
        self.assertIn("trail-loop", messages[0].lower())

    def test_trail_loop_counter_resets_between_calls(self) -> None:
        loop_text = "\n".join(
            ["[0.0 -> 1.0] [S] opening"]
            + ["[1.0 -> 2.0] [S] here is the problem"] * 40
        )
        clean_text = "[0.0 -> 1.0] [S] opening\n[1.0 -> 2.0] [S] closing"
        transcriber = Transcriber(provider=_FakeProvider(loop_text))
        transcriber.transcribe(
            audio_path=None, model="x", profile="best", language=None, timeout_sec=0
        )
        self.assertGreater(transcriber.last_trail_loop_trim, 0)
        transcriber.provider = _FakeProvider(clean_text)  # type: ignore[attr-defined]
        transcriber.transcribe(
            audio_path=None, model="x", profile="best", language=None, timeout_sec=0
        )
        self.assertEqual(transcriber.last_trail_loop_trim, 0)
        self.assertEqual(transcriber.last_trail_loop_sample, "")


if __name__ == "__main__":
    unittest.main()
