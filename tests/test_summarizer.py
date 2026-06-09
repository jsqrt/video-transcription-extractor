"""Unit tests for the LLM-only Summarizer.

The summarizer recursively chunks the transcript and summarises each
chunk, then summarises the joined summaries. A transcript that fits in
one chunk takes a single call. There is no extractive fallback, no JSON
parsing, no grounding/refine pass. These tests guard:

* prompts get localised to the transcript's language (uk → uk
  instructions; en → en; other → English instructions with an explicit
  "transcript is in X" directive),
* temperature is forwarded to the client,
* mode='none' / empty transcript short-circuit cleanly,
* LLM unavailable / failure surface as exceptions (no silent fallback),
* a transcript spanning multiple chunks recurses (more than one call,
  and the language is forwarded to every call).
"""

from __future__ import annotations

import threading
import unittest

from app.models.types import (
    ProviderUnavailableError,
    SummarizationError,
    SummaryOptions,
    Transcript,
    Utterance,
)
from app.services.summarizer import Summarizer


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


class _FakeLLMClient:
    """Records every ``chat`` call and returns a canned markdown reply."""

    def __init__(self, reply: str = "Коротке резюме одним абзацом.\n") -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []
        self.temperatures: list[float] = []
        self.num_predict_floors: list = []
        self.available = True

    def is_available(self) -> bool:
        return self.available

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.0,
        num_predict_floor=None,
    ) -> str:
        self.calls.append((system_prompt, user_prompt))
        self.temperatures.append(temperature)
        self.num_predict_floors.append(num_predict_floor)
        return self.reply


class ShortCircuitTest(unittest.TestCase):
    def test_mode_none_returns_none(self) -> None:
        # When summarization is explicitly disabled we must not even probe
        # the LLM client — saves a network round-trip on bulk runs.
        client = _FakeLLMClient()
        summarizer = Summarizer(
            options=SummaryOptions(mode="none"), llm_client=client
        )
        result = summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["раз", "два"])),
            language="uk",
        )
        self.assertIsNone(result)
        self.assertEqual(client.calls, [])

    def test_empty_transcript_returns_none(self) -> None:
        client = _FakeLLMClient()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        result = summarizer.summarize(
            transcript=Transcript(utterances=()), language="uk"
        )
        self.assertIsNone(result)
        self.assertEqual(client.calls, [])


class LLMUnavailableTest(unittest.TestCase):
    def test_no_client_raises(self) -> None:
        # The pipeline catches ProviderUnavailableError and continues
        # without a summary file. Confirming we raise (rather than
        # silently degrade) is what makes that contract honest.
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=None
        )
        with self.assertRaises(ProviderUnavailableError):
            summarizer.summarize(
                transcript=Transcript(utterances=_utterances(["раз"])),
                language="uk",
            )

    def test_client_not_available_raises(self) -> None:
        client = _FakeLLMClient()
        client.available = False
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        with self.assertRaises(ProviderUnavailableError):
            summarizer.summarize(
                transcript=Transcript(utterances=_utterances(["раз"])),
                language="uk",
            )

    def test_empty_llm_reply_raises_summarization_error(self) -> None:
        # The LLM is "up" but gave back nothing usable — we want a
        # SummarizationError (not silent empty file), so the pipeline can
        # log a clear "summary failed" message.
        client = _FakeLLMClient(reply="   \n  ")
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        with self.assertRaises(SummarizationError):
            summarizer.summarize(
                transcript=Transcript(utterances=_utterances(["раз"])),
                language="uk",
            )


class LocalisedPromptTest(unittest.TestCase):
    def test_ukrainian_transcript_uses_native_uk_prompt(self) -> None:
        # Minimal mode: the SYSTEM prompt is the Ukrainian template (small
        # LLMs follow target-language instructions far better than "reply in
        # X"); the USER prompt is the raw chunk text itself, no scaffolding.
        client = _FakeLLMClient()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["Привіт"])),
            language="uk",
        )
        system, user = client.calls[0]
        self.assertIn("транскрипт", system.lower())
        self.assertIn("переказ", system.lower())
        # User prompt is exactly the transcript chunk.
        self.assertEqual(user.strip(), "Привіт")

    def test_english_transcript_uses_native_en_prompt(self) -> None:
        client = _FakeLLMClient()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["Hello"])),
            language="en",
        )
        system, user = client.calls[0]
        self.assertIn("transcript", system.lower())
        self.assertIn("retell", system.lower())
        self.assertEqual(user.strip(), "Hello")

    def test_other_language_uses_english_with_directive(self) -> None:
        # For languages we don't carry native prompts for, we use the English
        # system prompt plus an explicit "transcript is in X / write in X"
        # directive — now carried in the SYSTEM prompt. The user prompt stays
        # the raw chunk.
        client = _FakeLLMClient()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["Cześć"])),
            language="pl",
        )
        system, user = client.calls[0]
        # Directive mentions the resolved language name, not the ISO code.
        self.assertIn("Polish", system)
        self.assertEqual(user.strip(), "Cześć")

    def test_unknown_language_falls_back_to_generic(self) -> None:
        # If detection returned an ISO code we don't have a name mapping
        # for, we still ship a usable prompt — say "in {code}" in the system
        # directive rather than crashing or omitting it.
        client = _FakeLLMClient()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["..."])),
            language="xyz",
        )
        system, _ = client.calls[0]
        self.assertIn("xyz", system)


class TemperatureTest(unittest.TestCase):
    def test_low_temperature_is_forwarded(self) -> None:
        # We want near-deterministic output for facts but NOT greedy
        # decoding: temperature=0 was the dominant cause of small-model
        # paragraph-loop collapse on long Ukrainian transcripts. A small
        # noise floor keeps numbers/names stable while letting the decoder
        # escape the "repeat the same paragraph" basin. Guard against an
        # accidental drift back to 0.0 in a refactor.
        # A one-chunk transcript with a SHORT summary makes a single call at
        # 0.2: the body is under the title threshold, so no title call fires.
        client = _FakeLLMClient()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["раз"])),
            language="uk",
        )
        self.assertEqual(client.temperatures, [0.2])


class ResultShapeTest(unittest.TestCase):
    def test_short_summary_has_no_title(self) -> None:
        # A short summary body (under the title threshold) is returned as-is,
        # with no "## title" heading and no extra title call.
        client = _FakeLLMClient(reply="\n\nТест абзацу.\n\n")
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        result = summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["раз"])),
            language="uk",
        )
        self.assertEqual(result, "Тест абзацу.")
        self.assertEqual(len(client.calls), 1)


class NoPreambleInstructionTest(unittest.TestCase):
    """In minimal mode the SYSTEM prompt carries the 'output is ONLY the
    finished text' rule and the anti-fabrication rule; the USER prompt is the
    raw chunk. Guard those rules stay in the system prompt for both templates,
    and that the user prompt has no scaffolding the model could echo."""

    def _system_prompt(self, language: str) -> str:
        client = _FakeLLMClient()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["x"])),
            language=language,
        )
        return client.calls[0][0]

    def test_uk_prompt_forbids_preamble(self) -> None:
        system = self._system_prompt("uk")
        # Output-is-only-text rule (no preamble / commentary).
        self.assertIn("без преамбули", system)
        # Anti-fabrication rule must be present.
        self.assertIn("не вигадуєте факти", system)

    def test_en_prompt_forbids_preamble(self) -> None:
        system = self._system_prompt("en")
        self.assertIn("no preamble", system.lower())
        self.assertIn("never invent facts", system.lower())

    def test_uk_user_prompt_is_raw_chunk(self) -> None:
        # Minimal mode: the user prompt is EXACTLY the transcript chunk — no
        # field labels ("ДЕ:/ЩО:/ХТО:") or other scaffolding to echo back.
        client = _FakeLLMClient()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["Подія сталася."])),
            language="uk",
        )
        _, user = client.calls[0]
        self.assertEqual(user.strip(), "Подія сталася.")


class MinChunkFloorTest(unittest.TestCase):
    def test_floor_prevents_tiny_chunks(self) -> None:
        # Even with a tiny requested target, the floor keeps chunks large
        # enough to retain context (the small-chunk hallucination fix).
        import os
        client = _FakeLLMClient()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        # A transcript well under the 800 floor must stay a single chunk.
        sentences = [f"Речення номер {i} про подію." for i in range(30)]
        os.environ["DESCRIBELY_CHUNK_TOKENS"] = "50"
        os.environ.pop("DESCRIBELY_MIN_CHUNK_TOKENS", None)
        try:
            summarizer.summarize(
                transcript=Transcript(utterances=_utterances(sentences)),
                language="uk",
            )
        finally:
            os.environ.pop("DESCRIBELY_CHUNK_TOKENS", None)
        # Floor >> this transcript, so it stays ONE chunk → one summary call.
        # The canned summary is short, so no title call is added.
        self.assertEqual(len(client.calls), 1)


class FlatChunkingTest(unittest.TestCase):
    def test_single_chunk_makes_one_call(self) -> None:
        # A one-chunk transcript with a short summary: one stage-1 call, no
        # stitch, and no title (body under the threshold) = 1 call.
        client = _FakeLLMClient()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["Коротко. Все."])),
            language="uk",
        )
        self.assertEqual(len(client.calls), 1)

    def test_long_transcript_chunks_and_concatenates(self) -> None:
        # A transcript far larger than one chunk must trigger MULTIPLE
        # standalone per-chunk summary calls. There is no stitch call any
        # more — the per-chunk summaries are concatenated, so the call count
        # equals the chunk count (> 1 here).
        client = _FakeLLMClient(reply="Абзац про подію. Ще речення.\n")
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        # ~80 distinct sentences, each non-trivial, blows past the chunk
        # budget and forces multiple chunks.
        sentences = [
            f"У місті номер {i} стався обстріл, постраждали {i} людей сьогодні."
            for i in range(80)
        ]
        summarizer.summarize(
            transcript=Transcript(utterances=_utterances(sentences)),
            language="uk",
        )
        self.assertGreater(len(client.calls), 1)
        # Every call (summary AND title) must carry a Ukrainian system prompt
        # — the map-reduce bug we fixed was chunk calls dropping the language
        # and drifting to Russian. "українською" appears in both templates.
        for system, _user in client.calls:
            self.assertIn("українською", system.lower())


class IncrementalSummaryFileTest(unittest.TestCase):
    """The .summary.md file is written after the first chunk and appended
    after each subsequent one, so a cancel/crash never loses produced text."""

    def test_first_write_titles_with_bom_then_appends(self) -> None:
        import tempfile
        from pathlib import Path

        from app.services.summary_writer import IncrementalSummaryFile

        with tempfile.TemporaryDirectory() as td:
            video = Path(td) / "clip.mp4"
            inc = IncrementalSummaryFile(source_video=video)
            self.assertFalse(inc.started)
            self.assertFalse(inc.path.exists())

            inc.append("Перший абзац.")
            self.assertTrue(inc.started)
            self.assertTrue(inc.path.exists())
            raw = inc.path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # BOM on first write
            text = raw.decode("utf-8-sig")
            self.assertIn("# Summary: clip", text)
            self.assertIn("Перший абзац.", text)

            inc.append("Другий абзац.")
            text2 = inc.path.read_text(encoding="utf-8-sig")
            self.assertIn("Перший абзац.", text2)
            self.assertIn("Другий абзац.", text2)
            # Exactly one BOM (appends must not re-emit it).
            self.assertEqual(inc.path.read_bytes().count(b"\xef\xbb\xbf"), 1)

            # Empty / whitespace partials are ignored.
            before = inc.path.read_text(encoding="utf-8-sig")
            inc.append("   ")
            self.assertEqual(inc.path.read_text(encoding="utf-8-sig"), before)


class StreamingTest(unittest.TestCase):
    """The summarizer streams each chunk's summary via ``partial_callback``
    and stops cooperatively on ``cancel_event`` without raising — the basis
    for writing the .summary.md file incrementally and keeping it on cancel."""

    @staticmethod
    def _long_sentences() -> list[str]:
        return [
            f"Речення номер {i} про подію у місті номер {i} сьогодні вранці."
            for i in range(80)
        ]

    def test_partial_callback_fires_once_per_chunk(self) -> None:
        client = _FakeLLMClient(reply="Абзац про подію.\n")
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        partials: list[str] = []
        summarizer.summarize(
            transcript=Transcript(utterances=_utterances(self._long_sentences())),
            language="uk",
            partial_callback=partials.append,
        )
        # One streamed partial per chunk, and > 1 chunk. The canned summary
        # is short, so no title calls fire and partials == chat calls.
        self.assertGreater(len(partials), 1)
        self.assertEqual(len(partials), len(client.calls))
        self.assertTrue(all(p.strip() for p in partials))

    def test_cancel_stops_between_chunks_and_keeps_partials(self) -> None:
        cancel = threading.Event()

        class _CancelAfterFirst(_FakeLLMClient):
            def chat(self, *args, **kwargs):  # type: ignore[override]
                out = super().chat(*args, **kwargs)
                cancel.set()  # request cancel right after the first chunk
                return out

        client = _CancelAfterFirst(reply="Абзац про подію.\n")
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        partials: list[str] = []
        result = summarizer.summarize(
            transcript=Transcript(utterances=_utterances(self._long_sentences())),
            language="uk",
            partial_callback=partials.append,
            cancel_event=cancel,
        )
        # Stopped after the first chunk: one chat call, one streamed partial
        # (the canned summary is short, so no title call is added).
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(partials), 1)
        # Returned the partial digest (no raise, no None) so the streamed
        # file is honoured rather than discarded.
        self.assertEqual(result, "Абзац про подію.")


class TitleStepTest(unittest.TestCase):
    """Stage 3: a long-enough chunk summary gets a '## title' heading."""

    def test_long_summary_gets_title_heading(self) -> None:
        body = "Подія сталася у місті, постраждали люди. " * 6  # > 200 chars

        class _TitleAware(_FakeLLMClient):
            def chat(self, system_prompt, user_prompt, *, temperature=0.0,
                     num_predict_floor=None):
                self.calls.append((system_prompt, user_prompt))
                self.temperatures.append(temperature)
                is_title = ("заголовок" in system_prompt
                            or "title" in system_prompt.lower())
                return "Подія в місті" if is_title else body

        client = _TitleAware()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        result = summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["раз"])),
            language="uk",
        )
        self.assertEqual(len(client.calls), 2)  # summary + title
        self.assertTrue(result.startswith("## Подія в місті\n\n"))
        self.assertIn(body.strip(), result)


class ForeignScriptGuardTest(unittest.TestCase):
    """qwen2.5 drifts into Chinese/Russian; the guard retries, then strips."""

    def test_cjk_drift_is_retried_then_clean(self) -> None:
        class _DriftOnce(_FakeLLMClient):
            def __init__(self):
                super().__init__()
                self._left = 1

            def chat(self, system_prompt, user_prompt, *, temperature=0.0,
                     num_predict_floor=None):
                self.calls.append((system_prompt, user_prompt))
                self.temperatures.append(temperature)
                if self._left > 0:
                    self._left -= 1
                    return "Подія сталася 触感 текст."  # contains CJK
                return "Подія сталася, все добре."

        client = _DriftOnce()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        result = summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["раз"])),
            language="uk",
        )
        # First call drifted (CJK) → one retry → clean. No CJK survives.
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(any("一" <= ch <= "鿿" for ch in result))
        self.assertEqual(result, "Подія сталася, все добре.")

    def test_persistent_cjk_is_stripped(self) -> None:
        class _AlwaysDrift(_FakeLLMClient):
            def chat(self, system_prompt, user_prompt, *, temperature=0.0,
                     num_predict_floor=None):
                self.calls.append((system_prompt, user_prompt))
                self.temperatures.append(temperature)
                return "Подія сталася 触感 текст."

        client = _AlwaysDrift()
        summarizer = Summarizer(
            options=SummaryOptions(mode="ollama"), llm_client=client
        )
        result = summarizer.summarize(
            transcript=Transcript(utterances=_utterances(["раз"])),
            language="uk",
        )
        # 1 call + 2 retries, all drift → CJK stripped from the final text.
        self.assertEqual(len(client.calls), 3)
        self.assertFalse(any("一" <= ch <= "鿿" for ch in result))
        self.assertIn("Подія сталася", result)
        self.assertIn("текст", result)


if __name__ == "__main__":
    unittest.main()
