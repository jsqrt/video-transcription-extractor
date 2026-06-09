"""Sentence-boundary chunking for the summarizer.

Splits text into chunks of at most a target token budget, where every
chunk boundary falls **after a sentence terminator** — never mid-sentence.
There is no overlap between chunks: the summarizer's recursive design
(chunk → summarise → re-chunk the summaries) does not need cross-chunk
context the way the old map-reduce splice did.

Token counts come from :func:`app.services.tokenization.count_tokens`.
"""

from __future__ import annotations

import re
from typing import List

from app.services.tokenization import count_tokens

# Sentence terminator followed by whitespace. We split AFTER the
# terminator so each piece keeps its closing punctuation. A short list of
# Ukrainian abbreviations ("м.", "вул." …) is guarded so we don't split
# on the period inside them. This is a pragmatic splitter, not a full NLP
# sentence tokenizer — good enough because the only consumer is chunk
# sizing, where an occasional imperfect boundary is harmless.
_ABBREVIATIONS = (
    "м.", "вул.", "просп.", "пл.", "буд.", "кв.", "обл.", "р.", "рр.",
    "ст.", "грн.", "млн.", "млрд.", "тис.", "им.", "т.", "д.", "п.",
)

_SENTENCE_END_RE = re.compile(r"([.!?…]+)(\s+|$)")


def split_sentences(text: str) -> List[str]:
    """Split ``text`` into sentences, keeping terminal punctuation.

    Boundaries are placed after ``.!?…`` runs that are followed by
    whitespace, unless the token immediately before the period is a known
    abbreviation. Whitespace-only fragments are dropped.
    """
    return [s for s, _start, _end in split_sentences_with_spans(text)]


def split_sentences_with_spans(text: str):
    """Like :func:`split_sentences` but also return original offsets.

    Returns a list of ``(sentence, start, end)`` where ``start``/``end``
    are indices into ``text`` covering that sentence INCLUDING the
    trailing whitespace up to the next sentence. Slicing ``text`` by these
    offsets reproduces the original verbatim — used by the seam stitcher
    to preserve paragraph breaks without searching for sentence content
    (which misfires when a sentence repeats).
    """
    spans = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        end = match.end(1)  # index just past the punctuation run
        candidate = text[start:end]
        # Don't break if the last whitespace-delimited token is an
        # abbreviation like "м." — that period isn't a sentence end.
        last_token = candidate.rsplit(None, 1)[-1] if candidate.split() else ""
        if last_token.lower() in _ABBREVIATIONS:
            continue
        # Don't break when a CAPITAL LETTER sits immediately before the
        # terminator — that's an initial ("Andrew E.") or an acronym, not a
        # sentence end. Without this, "Andrew E. Kramer …" splits after the
        # initial, stranding "Andrew E." at one chunk's tail and the rest in
        # the next. Over-merging an acronym-final sentence is harmless here:
        # the only consumer is chunk sizing.
        punct_start = match.start(1)
        if punct_start > 0:
            prev_char = text[punct_start - 1]
            if prev_char.isalpha() and prev_char.isupper():
                continue
        piece = text[start:match.end()].strip()
        if piece:
            spans.append((piece, start, match.end()))
        start = match.end()
    tail = text[start:]
    if tail.strip():
        spans.append((tail.strip(), start, len(text)))
    return spans


def chunk_text(
    text: str,
    target_tokens: int,
    overlap_tokens: int = 0,
) -> List[str]:
    """Split ``text`` into chunks of at most ``target_tokens`` tokens.

    Greedily packs whole sentences into a chunk until adding the next one
    would exceed ``target_tokens``, then starts a new chunk. Chunks never
    split a sentence. A single sentence longer than the budget becomes its
    own (over-budget) chunk rather than being cut — the LLM tolerates a
    slightly oversized chunk far better than a severed sentence.

    When ``overlap_tokens`` > 0, each new chunk is seeded with the trailing
    ~``overlap_tokens`` tokens (whole sentences) of the chunk just flushed,
    so adjacent chunks share their boundary content. A downstream stitcher
    uses that shared region as an anchor to merge per-chunk summaries
    without guessing where they join. Overlap is clamped below
    ``target_tokens`` so a chunk is never pure overlap.
    """
    sentences = split_sentences(text)
    if not sentences:
        return []

    overlap_tokens = max(0, min(overlap_tokens, target_tokens - 1))

    chunks: List[str] = []
    buffer: List[str] = []
    buffer_tokens = 0
    last_seed: List[str] = []  # overlap sentences carried into the buffer

    def _overlap_seed(flushed: List[str]) -> tuple[List[str], int]:
        """Trailing whole sentences of ``flushed`` summing to ~overlap."""
        if overlap_tokens <= 0:
            return [], 0
        tail: List[str] = []
        tail_tokens = 0
        for sent in reversed(flushed):
            t = count_tokens(sent)
            if tail and tail_tokens + t > overlap_tokens:
                break
            tail.insert(0, sent)
            tail_tokens += t
        return tail, tail_tokens

    for sentence in sentences:
        sent_tokens = count_tokens(sentence)
        # Flush the current buffer before this sentence would overflow it,
        # but only if the buffer already holds something (otherwise a lone
        # oversized sentence would produce an empty chunk).
        if buffer and buffer_tokens + sent_tokens > target_tokens:
            chunks.append(" ".join(buffer))
            buffer, buffer_tokens = _overlap_seed(buffer)
            last_seed = list(buffer)
        buffer.append(sentence)
        buffer_tokens += sent_tokens

    if buffer:
        # Avoid emitting a final chunk that is nothing but the overlap seed
        # of the previous one (the tail sentences all fit in the seed and no
        # new sentence followed it). Compare against the EXACT seed — a
        # legitimate final chunk that merely happens to be a substring of the
        # previous chunk must still be emitted, which the old substring test
        # would have dropped.
        if buffer != last_seed:
            chunks.append(" ".join(buffer))

    return chunks


__all__ = ["chunk_text", "split_sentences", "split_sentences_with_spans"]
