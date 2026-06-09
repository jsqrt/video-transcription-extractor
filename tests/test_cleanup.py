"""Unit tests for the rule-based cleanup pipeline.

These tests validate the invariants that make the cleanup safe:

* exact duplicates collapse within the dup window
* rolling chunk-repeats are stitched by dropping the overlapping prefix
* sentence join rule C protects against cross-speaker merges
* filler-run compression only touches short, repeated tokens
* the whole rule-based pass is a word-subsequence of the original
  (never adds a word, never paraphrases)

See ``app/services/cleanup.py`` for the underlying logic.
"""

from __future__ import annotations

import unittest

from app.models.types import Transcript, Utterance
from app.services.cleanup import (
    SHINGLE_OVERLAP_THRESHOLD,
    collapse_filler_runs,
    dedup_exact,
    is_word_subsequence,
    join_broken_sentences,
    merge_rolling_overlap,
    rule_based_cleanup,
)


def _utt(
    text: str,
    *,
    start: float,
    end: float,
    speaker: str = "SPEAKER_1",
) -> Utterance:
    return Utterance(speaker=speaker, text=text, start_sec=start, end_sec=end)


def _texts(utterances) -> list[str]:
    return [u.text for u in utterances]


class DedupExactTest(unittest.TestCase):
    def test_drops_exact_duplicate_within_window(self) -> None:
        utterances = [
            _utt("Це велике відкриття.", start=0.0, end=2.0),
            _utt("Це велике відкриття.", start=2.1, end=4.0),
            _utt("А тепер щось інше.", start=4.1, end=6.0),
        ]
        cleaned = dedup_exact(utterances)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0].text, "Це велике відкриття.")
        self.assertEqual(cleaned[1].text, "А тепер щось інше.")

    def test_normalises_case_punctuation_whitespace(self) -> None:
        utterances = [
            _utt("Дивіться сюди.", start=0.0, end=2.0),
            _utt("  дивіться СЮДИ!  ", start=2.1, end=4.0),
        ]
        cleaned = dedup_exact(utterances)
        self.assertEqual(len(cleaned), 1)

    def test_keeps_duplicate_outside_window(self) -> None:
        utterances = [
            _utt("одна фраза.", start=0.0, end=2.0),
            _utt("одна фраза.", start=100.0, end=102.0),
        ]
        cleaned = dedup_exact(utterances)
        self.assertEqual(len(cleaned), 2)

    def test_does_not_dedup_across_speakers(self) -> None:
        utterances = [
            _utt("хочу чаю", start=0.0, end=2.0, speaker="SPEAKER_1"),
            _utt("хочу чаю", start=2.1, end=4.0, speaker="SPEAKER_2"),
        ]
        cleaned = dedup_exact(utterances)
        self.assertEqual(len(cleaned), 2)


class MergeRollingOverlapTest(unittest.TestCase):
    def test_strips_repeated_prefix(self) -> None:
        # Whisper-style rolling repeat: most of the second utterance's
        # head parrots the tail of the first utterance verbatim, with
        # only a short new tail at the end.
        prev = (
            "Вони розповідали про Ормузьку протоку "
            "та її стратегічне значення минулого тижня."
        )
        curr_repeat_start = (
            "розповідали про Ормузьку протоку та її стратегічне "
            "значення минулого тижня. Тож вартість страховки знову виросла."
        )
        utterances = [
            _utt(prev, start=0.0, end=3.0),
            _utt(curr_repeat_start, start=3.2, end=6.0),
        ]
        cleaned = merge_rolling_overlap(utterances)
        self.assertEqual(len(cleaned), 2)
        self.assertNotIn("Ормузьку протоку", cleaned[1].text)
        self.assertIn("страховки знову виросла", cleaned[1].text)

    def test_leaves_unrelated_utterances_untouched(self) -> None:
        utterances = [
            _utt("спершу одне речення", start=0.0, end=2.0),
            _utt("абсолютно інша тема", start=2.1, end=4.0),
        ]
        cleaned = merge_rolling_overlap(utterances)
        self.assertEqual(_texts(cleaned), _texts(utterances))

    def test_overlap_below_threshold_is_ignored(self) -> None:
        utterances = [
            _utt("альфа бета гамма дельта епсилон", start=0.0, end=2.0),
            _utt("дельта епсилон дзета ета тета йота", start=2.1, end=4.0),
        ]
        cleaned = merge_rolling_overlap(utterances)
        # Only 2-word overlap → below 3-word shingle size, nothing collapses.
        self.assertEqual(_texts(cleaned), _texts(utterances))
        # And sanity: the threshold is actually the default we expect.
        self.assertGreater(SHINGLE_OVERLAP_THRESHOLD, 0.5)


class JoinBrokenSentencesTest(unittest.TestCase):
    def test_merges_mid_sentence_pair_same_speaker(self) -> None:
        utterances = [
            _utt("Я думаю, що вартість", start=0.0, end=2.0),
            _utt("виросла вдвічі.", start=2.1, end=4.0),
        ]
        cleaned = join_broken_sentences(utterances)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0].text, "Я думаю, що вартість виросла вдвічі.")
        # End time follows the second utterance so chapter boundaries stay sane.
        self.assertEqual(cleaned[0].end_sec, 4.0)

    def test_does_not_merge_across_speakers(self) -> None:
        utterances = [
            _utt("Я думаю, що вартість", start=0.0, end=2.0, speaker="SPEAKER_1"),
            _utt("виросла вдвічі.", start=2.1, end=4.0, speaker="SPEAKER_2"),
        ]
        cleaned = join_broken_sentences(utterances)
        self.assertEqual(len(cleaned), 2, "Different speakers must never merge")

    def test_does_not_merge_when_prev_is_terminated(self) -> None:
        utterances = [
            _utt("Це закінчене речення.", start=0.0, end=2.0),
            _utt("нове речення починається з малої.", start=2.1, end=4.0),
        ]
        cleaned = join_broken_sentences(utterances)
        self.assertEqual(len(cleaned), 2)

    def test_merges_when_next_starts_with_continuation(self) -> None:
        utterances = [
            _utt("Вони обговорили ринок нафти", start=0.0, end=2.0),
            _utt("але не дійшли згоди.", start=2.1, end=4.0),
        ]
        cleaned = join_broken_sentences(utterances)
        self.assertEqual(len(cleaned), 1)
        self.assertIn("але не дійшли згоди", cleaned[0].text)

    def test_does_not_merge_when_next_starts_with_capitalised_new_sentence(self) -> None:
        utterances = [
            _utt("Вони обговорили ринок нафти", start=0.0, end=2.0),
            _utt("Наступна тема - війна", start=2.1, end=4.0),
        ]
        cleaned = join_broken_sentences(utterances)
        self.assertEqual(len(cleaned), 2)


class CollapseFillerRunsTest(unittest.TestCase):
    def test_collapses_three_plus_identical_fillers(self) -> None:
        utterances = [
            _utt("так, так, так, дивіться", start=0.0, end=2.0),
        ]
        cleaned = collapse_filler_runs(utterances)
        # One "так" remains; the word "дивіться" survives.
        lowered = cleaned[0].text.lower()
        self.assertLessEqual(lowered.count("так"), 1)
        self.assertIn("дивіться", cleaned[0].text)

    def test_preserves_two_in_a_row(self) -> None:
        utterances = [
            _utt("так, так, дивіться", start=0.0, end=2.0),
        ]
        cleaned = collapse_filler_runs(utterances)
        # Two is below the threshold; keep as-is.
        self.assertEqual(cleaned[0].text, "так, так, дивіться")

    def test_does_not_collapse_long_words(self) -> None:
        utterances = [
            _utt(
                "партнери партнери партнери партнери",
                start=0.0,
                end=2.0,
            ),
        ]
        cleaned = collapse_filler_runs(utterances)
        # "партнери" is > 4 letters → untouched.
        self.assertEqual(
            cleaned[0].text, "партнери партнери партнери партнери"
        )


class IsWordSubsequenceTest(unittest.TestCase):
    def test_identity_is_subsequence(self) -> None:
        self.assertTrue(is_word_subsequence("Hello world", "hello world"))

    def test_removing_words_is_allowed(self) -> None:
        self.assertTrue(
            is_word_subsequence(
                "Ми пішли в магазин купити хліба",
                "пішли купити хліба",
            )
        )

    def test_adding_a_word_is_rejected(self) -> None:
        self.assertFalse(
            is_word_subsequence(
                "Ми пішли в магазин",
                "Ми пішли швидко в магазин",
            )
        )

    def test_reordering_is_rejected(self) -> None:
        self.assertFalse(
            is_word_subsequence(
                "раз два три",
                "три два раз",
            )
        )

    def test_punctuation_and_case_are_ignored(self) -> None:
        self.assertTrue(
            is_word_subsequence("HELLO, world!", "hello world")
        )


class RuleBasedCleanupTest(unittest.TestCase):
    def test_full_pipeline_produces_subsequence(self) -> None:
        original_texts = [
            "Іран контролює Ормузьку протоку.",
            "Іран контролює Ормузьку протоку.",  # dup
            "Також вони збирають плату",  # mid-sentence
            "два мільйони доларів за прохід.",
            "так, так, так, дивіться далі.",
        ]
        transcript = Transcript(
            utterances=tuple(
                _utt(text, start=i * 2.0, end=i * 2.0 + 2.0)
                for i, text in enumerate(original_texts)
            )
        )
        cleaned = rule_based_cleanup(transcript)

        # The whole document must be a word-subsequence of the original.
        joined_before = " ".join(original_texts)
        joined_after = " ".join(u.text for u in cleaned.utterances)
        self.assertTrue(
            is_word_subsequence(joined_before, joined_after),
            f"rule-based cleanup must not add words.\n"
            f"before: {joined_before!r}\n"
            f"after: {joined_after!r}",
        )

        # Dedup + join took effect: we expect fewer utterances than input.
        self.assertLess(len(cleaned.utterances), len(original_texts))

    def test_does_not_merge_different_speakers_even_through_full_pipeline(self) -> None:
        """The multi-speaker protection is essential for C (join) and A (dedup)."""
        transcript = Transcript(
            utterances=(
                _utt("Я думаю, що вартість", start=0.0, end=2.0, speaker="SPEAKER_1"),
                _utt("виросла вдвічі.", start=2.1, end=4.0, speaker="SPEAKER_2"),
                _utt("хочу чаю", start=4.1, end=6.0, speaker="SPEAKER_1"),
                _utt("хочу чаю", start=6.1, end=8.0, speaker="SPEAKER_2"),
            )
        )
        cleaned = rule_based_cleanup(transcript)
        # Two speakers can never be merged into one utterance.
        self.assertEqual(len(cleaned.utterances), 4)
        # And neither of the two "хочу чаю" lines got deduped.
        hochu = [u for u in cleaned.utterances if u.text.strip().startswith("хочу")]
        self.assertEqual(len(hochu), 2)

    def test_empty_transcript_is_fine(self) -> None:
        cleaned = rule_based_cleanup(Transcript(utterances=()))
        self.assertEqual(cleaned.utterances, ())


if __name__ == "__main__":
    unittest.main()
