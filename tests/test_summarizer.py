from __future__ import annotations

import unittest

from app.models.types import SummaryOptions, Transcript, Utterance
from app.services.chapterizer import build_chapters
from app.services.summarizer import (
    ChapterSummary,
    ExtractiveSummarizer,
    Fact,
    Intent,
    Summarizer,
    SummaryResult,
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


class ExtractiveSummarizerTest(unittest.TestCase):
    def test_empty_text(self) -> None:
        self.assertEqual(
            ExtractiveSummarizer().summarize_block("", max_sentences=3), ""
        )

    def test_returns_up_to_n(self) -> None:
        text = (
            "Ринок нафти знову зростає після заяви Трампа. "
            "Аналітики вважають, що Іран зберігає контроль над протокою. "
            "Ціна за барель тримається біля ста доларів. "
            "Це може вплинути на всю світову торгівлю. "
            "Турція та Китай спостерігають за ситуацією."
        )
        prose = ExtractiveSummarizer().summarize_block(text, max_sentences=3)
        self.assertIsInstance(prose, str)
        self.assertTrue(prose.strip())
        self.assertLess(len(prose), len(text))

    def test_short_text_returns_all(self) -> None:
        text = "Короткий уривок. Ще один."
        prose = ExtractiveSummarizer().summarize_block(text, max_sentences=5)
        self.assertIn("Короткий уривок", prose)
        self.assertIn("Ще один", prose)

    def test_deterministic(self) -> None:
        text = (
            "Перше речення про ринок нафти. "
            "Друге речення також про нафту. "
            "Третє речення зовсім про інше."
        )
        a = ExtractiveSummarizer().summarize_block(text, max_sentences=2)
        b = ExtractiveSummarizer().summarize_block(text, max_sentences=2)
        self.assertEqual(a, b)

    def test_derive_title_uses_keywords(self) -> None:
        text = (
            "market market market ukraine ukraine growth growth growth "
            "policy policy reform reform reform"
        )
        title = ExtractiveSummarizer().derive_title(text, max_words=3)
        words = title.split()
        self.assertLessEqual(len(words), 3)
        self.assertTrue(title.strip())

    def test_extract_facts_picks_sentences_with_digits(self) -> None:
        utterances = _utterances(
            [
                "Ціна сягнула 100 доларів за барель.",
                "Це просто думка спікера без чисел.",
                "У протоці зараз 4 кораблі під прапорами Панами.",
            ]
        )
        facts = ExtractiveSummarizer().extract_facts(utterances)
        texts = [f.text for f in facts]
        self.assertTrue(
            any("100" in text for text in texts),
            f"expected a fact with '100' in {texts!r}",
        )
        self.assertTrue(
            any("4 кораблі" in text for text in texts),
            f"expected a fact with '4 кораблі' in {texts!r}",
        )
        # All facts must be Fact instances with a timecode string.
        for fact in facts:
            self.assertIsInstance(fact, Fact)

    def test_extract_intents_picks_imperative_markers(self) -> None:
        utterances = _utterances(
            [
                "Трамп обіцяв оголосити нові санкції.",
                "Просто статистика без намірів.",
                "Ринок має відреагувати дуже швидко.",
            ]
        )
        intents = ExtractiveSummarizer().extract_intents(utterances)
        texts = [i.text for i in intents]
        self.assertTrue(
            any("обіцяв" in t for t in texts),
            f"expected an intent with 'обіцяв' in {texts!r}",
        )
        self.assertTrue(
            any("має відреагувати" in t for t in texts),
            f"expected an intent with 'має відреагувати' in {texts!r}",
        )
        for intent in intents:
            self.assertIsInstance(intent, Intent)


class _FakeLLMClient:
    """Stand-in that returns a canned chapter payload and a canned synthesis payload."""

    def __init__(
        self,
        chapter_payload: dict,
        synthesis_payload: dict,
    ) -> None:
        self.chapter_payload = chapter_payload
        self.synthesis_payload = synthesis_payload
        self.calls: list[tuple[str, str]] = []
        self.available = True

    def is_available(self) -> bool:
        return self.available

    def chat_json(
        self, system_prompt: str, user_prompt: str, *, temperature: float = 0.2
    ) -> dict:
        self.calls.append((system_prompt, user_prompt))
        # The synthesis call includes the full transcript as ground truth —
        # the per-chapter call does not.
        if "FULL TRANSCRIPT" in user_prompt:
            return self.synthesis_payload
        return self.chapter_payload


class SummarizerFacadeTest(unittest.TestCase):
    def test_mode_none_returns_empty(self) -> None:
        options = SummaryOptions(mode="none")
        summarizer = Summarizer(options=options)
        transcript = Transcript(
            utterances=_utterances(["раз раз раз", "два два два"])
        )
        result = summarizer.summarize(transcript=transcript, chapters=[])
        self.assertIsInstance(result, SummaryResult)
        self.assertEqual(result.overview, "")
        self.assertEqual(result.per_chapter, ())
        self.assertEqual(result.key_facts, ())
        self.assertEqual(result.intents, ())

    def test_extractive_end_to_end(self) -> None:
        sentences = [
            "Іран тримає Ормузьку протоку під контролем.",
            "Трамп оголосив нові умови.",
            "Нафта тримається біля 100 доларів за барель.",
            "Аналітики очікують нової угоди.",
            "Китай уважно стежить за ситуацією.",
            "Турція оголосила підвищення мита на Босфорі.",
            "Світова торгова система має змінитися.",
            "США втрачають контроль над ключовими протоками.",
        ]
        transcript = Transcript(utterances=_utterances(sentences * 4))
        chapters = build_chapters(transcript)
        options = SummaryOptions(
            mode="extractive",
            per_chapter_sentences=2,
            overview_sentences=3,
        )
        result = Summarizer(options=options).summarize(
            transcript=transcript, chapters=chapters
        )
        self.assertIsInstance(result.overview, str)
        self.assertTrue(result.overview.strip())
        self.assertEqual(len(result.per_chapter), len(chapters))
        for chapter_summary in result.per_chapter:
            self.assertIsInstance(chapter_summary, ChapterSummary)
            self.assertTrue(chapter_summary.refined_title.strip())
        # Digit-bearing facts should survive the extractive pass.
        self.assertTrue(any("100" in f.text for f in result.key_facts))

    def test_llm_structured_response_populates_all_sections(self) -> None:
        options = SummaryOptions(mode="ollama")
        client = _FakeLLMClient(
            chapter_payload={
                "title": "Нафта і протока",
                "bullet": "Коротко про нафту.",
            },
            synthesis_payload={
                "overview": "Загальний огляд відео.",
                "key_facts": [
                    "[00:12] $2M за прохід",
                    {"text": "Нафта $100 за барель", "timecode": "[00:30]"},
                ],
                "intents": [
                    "[01:30] США планують оголосити нові умови",
                ],
            },
        )
        summarizer = Summarizer(options=options, llm_client=client)
        transcript = Transcript(
            utterances=_utterances(
                [
                    "Перше речення про нафту і протоку.",
                    "Друге речення про торгівлю і Ормуз.",
                    "Третє речення про вплив на ринок.",
                ]
            )
        )
        chapters = build_chapters(transcript)
        result = summarizer.summarize(transcript=transcript, chapters=chapters)

        self.assertEqual(result.overview, "Загальний огляд відео.")
        self.assertEqual(len(result.per_chapter), len(chapters))
        for chapter_summary in result.per_chapter:
            self.assertEqual(chapter_summary.refined_title, "Нафта і протока")
            self.assertEqual(chapter_summary.summary, "Коротко про нафту.")

        self.assertEqual(len(result.key_facts), 2)
        self.assertEqual(result.key_facts[0].timecode, "[00:12]")
        self.assertEqual(result.key_facts[0].text, "$2M за прохід")
        self.assertEqual(result.key_facts[1].timecode, "[00:30]")
        self.assertEqual(result.key_facts[1].text, "Нафта $100 за барель")

        self.assertEqual(len(result.intents), 1)
        self.assertEqual(result.intents[0].timecode, "[01:30]")
        self.assertEqual(
            result.intents[0].text,
            "США планують оголосити нові умови",
        )
        # One call per chapter + one synthesis call.
        self.assertEqual(len(client.calls), len(chapters) + 1)

    def test_llm_unreachable_falls_back_to_extractive(self) -> None:
        options = SummaryOptions(mode="ollama")
        summarizer = Summarizer(options=options, llm_client=None)
        transcript = Transcript(
            utterances=_utterances(
                [
                    "Ринок нафти. Ціни зростають 100 доларів.",
                    "Турція хоче збирати плату.",
                    "Це має змінити торгову систему.",
                ]
            )
        )
        chapters = build_chapters(transcript)
        result = summarizer.summarize(transcript=transcript, chapters=chapters)
        self.assertTrue(
            bool(result.overview)
            or any(cs.summary for cs in result.per_chapter)
        )


if __name__ == "__main__":
    unittest.main()
