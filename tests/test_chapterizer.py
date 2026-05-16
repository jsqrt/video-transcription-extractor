from __future__ import annotations

import unittest

from app.models.types import Transcript, Utterance
from app.services.chapterizer import build_chapters


def _mk_utterance(text: str, start: float) -> Utterance:
    return Utterance(speaker="SPEAKER_1", text=text, start_sec=start, end_sec=start + 1.0)


class BuildChaptersTest(unittest.TestCase):
    def test_empty_transcript_returns_empty_chapters(self) -> None:
        self.assertEqual(build_chapters(Transcript()), [])

    def test_short_transcript_single_chapter(self) -> None:
        utterances = tuple(
            _mk_utterance(f"sentence number {i}", i * 1.0) for i in range(3)
        )
        chapters = build_chapters(Transcript(utterances=utterances))
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].start_index, 0)
        self.assertEqual(chapters[0].end_index, 3)

    def test_keyword_title_uses_non_stopwords(self) -> None:
        # Repeat keywords so they dominate. Without stopword filtering
        # the title would be contaminated by pronouns.
        utterances = tuple(
            _mk_utterance("the market in Ukraine is growing very fast", i * 1.0)
            for i in range(3)
        )
        chapters = build_chapters(Transcript(utterances=utterances), title_style="keywords")
        title = chapters[0].title.lower()
        self.assertTrue(
            any(kw in title for kw in ("market", "ukraine", "growing")),
            msg=f"Expected meaningful keyword in title, got: {title}",
        )

    def test_snippet_title_style(self) -> None:
        utterances = tuple(
            _mk_utterance("The market in Ukraine is growing very fast", i * 1.0)
            for i in range(3)
        )
        chapters = build_chapters(Transcript(utterances=utterances), title_style="snippet")
        title = chapters[0].title.lower()
        # Snippet style should contain at least two lowercase keyword-capitalized words joined by space.
        self.assertTrue("market" in title or "ukraine" in title, msg=title)

    def test_multiple_chapters_for_long_transcript(self) -> None:
        utterances: list[Utterance] = []
        # Build two thematic blocks with different vocabulary.
        for i in range(30):
            utterances.append(_mk_utterance(
                "космос планета ракета земля орбіта астронавт супутник політ",
                i * 1.0,
            ))
        for i in range(30):
            utterances.append(_mk_utterance(
                "футбол гол стадіон тренер команда матч чемпіонат вболівальник",
                (30 + i) * 1.0,
            ))
        chapters = build_chapters(Transcript(utterances=tuple(utterances)))
        self.assertGreaterEqual(len(chapters), 2, msg="expected at least 2 chapters")
        # First chapter should lean towards space vocabulary.
        first_title = chapters[0].title.lower()
        last_title = chapters[-1].title.lower()
        space_hit = any(word in first_title for word in ("космос", "планета", "ракета", "орбіта"))
        sports_hit = any(word in last_title for word in ("футбол", "стадіон", "команда", "матч"))
        self.assertTrue(space_hit or sports_hit,
                        msg=f"Neither space nor sports vocabulary in titles: {first_title!r} / {last_title!r}")


if __name__ == "__main__":
    unittest.main()
