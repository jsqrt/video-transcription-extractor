"""Unit tests for sentence-boundary chunking and token counting."""

from __future__ import annotations

import unittest

from app.services import tokenization
from app.services.chunking import chunk_text, split_sentences
from app.services.tokenization import count_tokens


class TokenizationTests(unittest.TestCase):
    def tearDown(self):
        tokenization.reset_tokenizer_cache()

    def test_count_tokens_empty(self):
        self.assertEqual(count_tokens(""), 0)

    def test_count_tokens_nonzero(self):
        # Whatever backend is active (real tokenizer or heuristic), a
        # normal sentence has a positive, sane token count.
        n = count_tokens("У Марганці окупанти вдарили БПЛА по вантажівці.")
        self.assertTrue(5 < n < 60, n)

    def test_count_tokens_fallback_when_no_tokenizer(self):
        # Force the fallback path and check the chars/2.0 heuristic.
        tokenization.reset_tokenizer_cache()
        original = tokenization._load_tokenizer
        tokenization._load_tokenizer = lambda: None
        try:
            self.assertEqual(count_tokens("a" * 20), 10)
        finally:
            tokenization._load_tokenizer = original
            tokenization.reset_tokenizer_cache()


class SplitSentencesTests(unittest.TestCase):
    def test_split_keeps_punctuation(self):
        out = split_sentences("Перше речення. Друге речення! Третє?")
        self.assertEqual(out, ["Перше речення.", "Друге речення!", "Третє?"])

    def test_split_does_not_break_on_abbreviation(self):
        # "м. Дніпро" must stay in one sentence, not split after "м."
        out = split_sentences("Сталося у м. Дніпро вчора ввечері. Кінець.")
        self.assertEqual(out, ["Сталося у м. Дніпро вчора ввечері.", "Кінець."])

    def test_split_handles_no_terminal_punctuation(self):
        out = split_sentences("Речення без крапки в кінці")
        self.assertEqual(out, ["Речення без крапки в кінці"])

    def test_split_empty(self):
        self.assertEqual(split_sentences(""), [])
        self.assertEqual(split_sentences("   "), [])


class ChunkTextTests(unittest.TestCase):
    def test_chunk_respects_sentence_boundaries(self):
        text = "Один. Два. Три. Чотири. П'ять."
        # Tiny budget forces multiple chunks; none may end mid-sentence.
        chunks = chunk_text(text, target_tokens=3)
        self.assertTrue(chunks)
        rejoined = " ".join(chunks)
        for sent in ["Один.", "Два.", "Три.", "Чотири.", "П'ять."]:
            self.assertIn(sent, rejoined)
        for c in chunks:
            self.assertTrue(c.strip().endswith((".", "!", "?", "…")), c)

    def test_chunk_packs_until_budget(self):
        text = "Один. Два. Три."
        chunks = chunk_text(text, target_tokens=1000)
        self.assertEqual(chunks, ["Один. Два. Три."])

    def test_chunk_oversized_single_sentence_not_split(self):
        # A single sentence larger than the budget becomes its own chunk
        # rather than being cut in half.
        long_sentence = " ".join(["слово"] * 200) + "."
        chunks = chunk_text(long_sentence, target_tokens=10)
        self.assertEqual(chunks, [long_sentence])

    def test_chunk_empty_text(self):
        self.assertEqual(chunk_text("", target_tokens=100), [])
        self.assertEqual(chunk_text("   ", target_tokens=100), [])

    def test_overlap_shares_boundary_sentences(self):
        # With overlap, the tail sentences of chunk N reappear at the
        # start of chunk N+1.
        text = " ".join(f"Речення номер {i} тут." for i in range(40))
        chunks = chunk_text(text, target_tokens=30, overlap_tokens=10)
        self.assertGreater(len(chunks), 1)
        for i in range(len(chunks) - 1):
            prev_last = chunks[i].split(". ")[-1].rstrip(".")
            self.assertIn(prev_last, chunks[i + 1])

    def test_overlap_zero_is_disjoint(self):
        # overlap_tokens=0 reproduces the original non-overlapping split.
        text = " ".join(f"Речення {i}." for i in range(40))
        a = chunk_text(text, target_tokens=30, overlap_tokens=0)
        b = chunk_text(text, target_tokens=30)
        self.assertEqual(a, b)

    def test_overlap_clamped_below_target(self):
        # Overlap larger than the target must not loop forever / empty out.
        text = " ".join(f"Речення {i}." for i in range(20))
        chunks = chunk_text(text, target_tokens=5, overlap_tokens=999)
        self.assertTrue(chunks)
        # Every original sentence still present somewhere.
        joined = " ".join(chunks)
        for i in range(20):
            self.assertIn(f"Речення {i}.", joined)


if __name__ == "__main__":
    unittest.main()
