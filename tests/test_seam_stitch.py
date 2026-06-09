"""Unit tests for the sequential fold-merge of per-chunk summaries.

The stitcher merges adjacent chunk-summaries by handing the model two
FULL fragments per step and re-chunking the merged result between steps
(see app/services/seam_stitch.py). These tests pin that contract: full
fragments go to the model, the merged reply carries forward, and the
final digest is the concatenation of every frozen re-chunk plus the
trailing carry.
"""

from __future__ import annotations

import unittest

from app.services.seam_stitch import stitch_summaries


class _MergeLLM:
    """Fake LLM: records merge prompts, returns a fixed merged text.

    By default it echoes ``A + B`` so we can check nothing is dropped; a
    custom ``reply`` (or ``reply_fn``) lets a test simulate the model
    collapsing a duplicate.
    """

    def __init__(self, reply=None, reply_fn=None) -> None:
        self.reply = reply
        self.reply_fn = reply_fn
        self.calls: list[tuple[str, str]] = []

    def chat(self, system_prompt, user_prompt, *, temperature=0.0,
             num_predict_floor=None) -> str:
        self.calls.append((system_prompt, user_prompt))
        if self.reply_fn is not None:
            return self.reply_fn(user_prompt)
        if self.reply is not None:
            return self.reply
        # Default: echo both fragments concatenated (no merging) so the
        # test can assert that all content survives the fold.
        # The user prompt embeds the two fragments verbatim.
        return user_prompt


class StitchSummariesTests(unittest.TestCase):
    def test_empty_returns_empty(self):
        llm = _MergeLLM()
        self.assertEqual(stitch_summaries([], llm), "")
        self.assertEqual(stitch_summaries(["   ", ""], llm), "")
        self.assertEqual(llm.calls, [])

    def test_single_summary_no_llm_call(self):
        llm = _MergeLLM()
        out = stitch_summaries(["Один абзац. Кінець."], llm)
        self.assertEqual(out, "Один абзац. Кінець.")
        self.assertEqual(llm.calls, [])  # nothing to merge

    def test_two_summaries_make_one_merge_call(self):
        llm = _MergeLLM(reply="ОБ'ЄДНАНО.")
        stitch_summaries(["Ліва частина. Кінець лівої.",
                          "Початок правої. Права частина."], llm)
        self.assertEqual(len(llm.calls), 1)

    def test_n_summaries_make_n_minus_one_merge_calls(self):
        # Sequential fold: three summaries → two merge steps.
        llm = _MergeLLM(reply="X.")
        stitch_summaries(["A. A2.", "B. B2.", "C. C2."], llm)
        self.assertEqual(len(llm.calls), 2)

    def test_full_fragments_sent_to_model(self):
        # The whole of both adjacent fragments must reach the model — not a
        # narrow seam window. This is what gives it local context to judge
        # duplicates vs distinct events.
        llm = _MergeLLM(reply="МЕРЖ.")
        left = "Л1. Л2. Л3. Л4. Л5_остання."
        right = "П1_перша. П2. П3. П4. П5."
        stitch_summaries([left, right], llm)
        _system, user = llm.calls[0]
        # Far body sentences are present (full fragment, not just boundary):
        self.assertIn("Л1.", user)
        self.assertIn("Л5_остання", user)
        self.assertIn("П1_перша", user)
        self.assertIn("П5.", user)

    def test_no_content_dropped_when_model_echoes(self):
        # With the echo LLM (no merging), every fact from every chunk must
        # survive into the final digest.
        llm = _MergeLLM()  # echoes the prompt (both fragments)
        out = stitch_summaries(
            ["Подія А у Дніпрі. 20 поранених.",
             "Подія Б у Львові. 5 загиблих.",
             "Подія В у Києві. 3 затримані."],
            llm,
        )
        for marker in ("Дніпрі", "20 поранених", "Львові", "5 загиблих",
                       "Києві", "3 затримані"):
            self.assertIn(marker, out)

    def test_merge_reply_carries_into_next_step(self):
        # The reply of step 1 must be the left fragment of step 2 — the
        # fold carries the running merge forward, it does not re-send the
        # original first summary.
        llm = _MergeLLM(reply_fn=lambda user: "МЕРЖ_РЕЗУЛЬТАТ. " + user[:0] + "Хвіст.")
        stitch_summaries(["Перший. A.", "Другий. B.", "Третій. C."], llm)
        # Second call's user prompt (FRAGMENT A) must contain the step-1
        # reply text, and the third summary must be FRAGMENT B.
        _sys2, user2 = llm.calls[1]
        self.assertIn("МЕРЖ_РЕЗУЛЬТАТ", user2)
        self.assertIn("Третій", user2)

    def test_empty_reply_falls_back_to_concatenation(self):
        # If the model returns nothing, we must not silently drop a chunk:
        # fall back to concatenating the two fragments.
        llm = _MergeLLM(reply="")
        out = stitch_summaries(["Лівий факт.", "Правий факт."], llm)
        self.assertIn("Лівий факт", out)
        self.assertIn("Правий факт", out)


if __name__ == "__main__":
    unittest.main()
