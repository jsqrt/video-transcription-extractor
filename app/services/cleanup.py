"""Rule-based and LLM-assisted cleanup of a Whisper transcript.

The goal is to remove *Whisper's own artefacts*: duplicate chunks that
appear on segment boundaries, rolling chunk-repeats where the start of
utterance N+1 parrots the end of N, and utterances broken mid-sentence.

The cleanup stays STRICTLY conservative:

* We never paraphrase. We never introduce new words.
* We never merge across speakers (diarization-aware).
* Rule (a) exact-normalize dedup removes exact repeats only.
* Rule (c) shingles partial-overlap stitches rolling repeats by dropping
  the already-seen prefix of the second utterance.
* Join-broken-sentence rule (A+C) merges only when the first utterance
  ends without a sentence-ending punctuation AND the second starts with
  a lowercase letter, a digit, or a well-known sentence continuer, AND
  the speakers match.
* Filler compression collapses runs of 3+ identical short tokens down
  to one, preserving user speech otherwise verbatim.

An optional LLM pass (``llm_cleanup``) is gated by a subsequence-diff
validator: the LLM's output must consist of words that all appear, in
order, in the original transcript. If the LLM added a word, we fall back
to the rule-based result instead.

This module has zero external dependencies (uses only stdlib ``re`` and
the internal ``app.models.types``) so the rule-based cleanup can run
inside MCP requests without pulling Ollama into the import graph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Literal, Optional, Sequence

from app.models.types import Transcript, Utterance


CleanMode = Literal["raw", "rule-based", "llm"]

# Two utterances whose start_secs differ by no more than this are
# considered "close" for dedup / rolling-overlap purposes.
DUP_WINDOW_SEC = 5.0

# Shingle size used for partial-overlap detection.
_SHINGLE_SIZE = 3
# Fraction of the tail's shingles that must appear in the head before
# we treat it as a rolling chunk-repeat.
SHINGLE_OVERLAP_THRESHOLD = 0.6
# How many words from each side we look at when computing shingle
# overlap. Keep this small — we only care about the boundary.
_OVERLAP_WINDOW_WORDS = 20

# Sentence-ending punctuation. Anything else at the end of an utterance
# is considered "mid-sentence" for the join rule.
_SENTENCE_END = {".", "?", "!", "…"}

# Lowercase Ukrainian function words that nearly always start a
# continuation rather than a fresh sentence.
_CONTINUATION_WORDS = frozenset({
    "і", "й", "та", "а", "але", "і", "що", "якщо", "бо", "тому",
    "отже", "проте", "однак", "хоч", "хоча", "адже", "тоді", "де",
    "коли", "як", "чи", "або", "чим", "які", "який", "яка", "яке",
    "ще", "тобто", "тож", "зате", "причому", "поки",
})

# Minimum number of consecutive identical filler tokens before we
# collapse them. (Having 1-2 "ну"s in a row is normal speech; 3+ is
# Whisper artifacting.)
_FILLER_COLLAPSE_THRESHOLD = 3

_WORD_RE = re.compile(r"[^\W_]+(?:['\u2019\u02BC][^\W_]+)*", re.UNICODE)


# --- text utilities ----------------------------------------------------------


def _normalize_for_dedup(text: str) -> str:
    """Canonical form used to compare two utterances for equality.

    Lowercased, punctuation stripped, whitespace collapsed. Two strings
    that differ only in capitalisation, trailing period, or a stray
    whitespace compare equal.
    """
    lowered = text.casefold()
    stripped = re.sub(r"[^\w\s]+", " ", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", stripped).strip()


def _words_lower(text: str) -> list[str]:
    return [m.group(0).casefold() for m in _WORD_RE.finditer(text)]


def _shingles(words: Sequence[str], size: int) -> list[tuple[str, ...]]:
    if len(words) < size:
        return []
    return [tuple(words[i : i + size]) for i in range(len(words) - size + 1)]


def _ends_mid_sentence(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return False
    return stripped[-1] not in _SENTENCE_END


def _first_word(text: str) -> str:
    match = _WORD_RE.search(text)
    return match.group(0) if match else ""


def _starts_with_continuation(text: str) -> bool:
    word = _first_word(text)
    if not word:
        return False
    # A digit-starting utterance ("2 мільйони…") is almost always a
    # continuation from a broken chunk ("Плата становить ,").
    if word[0].isdigit():
        return True
    if word[0].islower():
        return True
    return word.casefold() in _CONTINUATION_WORDS


def _within_dup_window(a: Utterance, b: Utterance) -> bool:
    if a.end_sec is None or b.start_sec is None:
        return True  # No timing info → be conservative and check anyway.
    return (b.start_sec - a.end_sec) <= DUP_WINDOW_SEC


# --- individual cleanup passes ----------------------------------------------


def dedup_exact(utterances: Sequence[Utterance]) -> list[Utterance]:
    """Drop utterances whose normalised text equals a recent neighbour.

    Window = ±5s (controlled by ``DUP_WINDOW_SEC``). Same speaker only.
    The first occurrence wins; later duplicates are removed.
    """
    result: list[Utterance] = []
    # Keep a rolling record of (normalised_text, speaker, end_sec).
    recent: list[tuple[str, str, Optional[float]]] = []
    for utt in utterances:
        key = _normalize_for_dedup(utt.text)
        if not key:
            result.append(utt)
            continue

        # Evict stale entries from `recent` (outside the window).
        if utt.start_sec is not None:
            cutoff = utt.start_sec - DUP_WINDOW_SEC
            recent = [(k, s, e) for (k, s, e) in recent if e is None or e >= cutoff]

        if any(k == key and s == utt.speaker for (k, s, _e) in recent):
            # Duplicate — skip it entirely.
            continue

        result.append(utt)
        recent.append((key, utt.speaker, utt.end_sec))
    return result


def merge_rolling_overlap(utterances: Sequence[Utterance]) -> list[Utterance]:
    """Stitch rolling chunk-repeats across adjacent utterances.

    Whisper sometimes emits:

        N   : "…Ormuz Strait. This is a critical corridor."
        N+1 : "corridor. This is a critical corridor. So oil prices go up."

    The head of N+1 parrots the tail of N. We rewrite N+1 to keep only
    the new tail ("So oil prices go up.").
    """
    result: list[Utterance] = []
    for utt in utterances:
        if not result:
            result.append(utt)
            continue

        prev = result[-1]
        if utt.speaker != prev.speaker or not _within_dup_window(prev, utt):
            result.append(utt)
            continue

        tail_words = _words_lower(prev.text)[-_OVERLAP_WINDOW_WORDS:]
        head_words = _words_lower(utt.text)[:_OVERLAP_WINDOW_WORDS]
        if len(tail_words) < _SHINGLE_SIZE or len(head_words) < _SHINGLE_SIZE:
            result.append(utt)
            continue

        tail_shingles = set(_shingles(tail_words, _SHINGLE_SIZE))
        head_shingles = _shingles(head_words, _SHINGLE_SIZE)
        if not tail_shingles or not head_shingles:
            result.append(utt)
            continue

        head_set = set(head_shingles)
        overlap = len(tail_shingles & head_set) / len(head_set)
        if overlap < SHINGLE_OVERLAP_THRESHOLD:
            result.append(utt)
            continue

        # Find last head shingle that is in the tail; strip up to its end.
        last_match = -1
        for idx, shingle in enumerate(head_shingles):
            if shingle in tail_shingles:
                last_match = idx
        if last_match < 0:
            result.append(utt)
            continue

        # Words consumed by the overlap = last_match + _SHINGLE_SIZE.
        consumed_words = last_match + _SHINGLE_SIZE
        rewritten = _drop_leading_words(utt.text, consumed_words)
        rewritten = rewritten.lstrip(" ,.;:—-")
        if not rewritten.strip():
            # Whole utterance was a repeat → drop it entirely.
            continue
        result.append(replace(utt, text=rewritten))
    return result


def _drop_leading_words(text: str, n: int) -> str:
    """Return ``text`` with the first ``n`` word-tokens dropped, preserving
    the original whitespace / punctuation of the remainder.
    """
    if n <= 0:
        return text
    matches = list(_WORD_RE.finditer(text))
    if n >= len(matches):
        return ""
    return text[matches[n].start():]


def join_broken_sentences(utterances: Sequence[Utterance]) -> list[Utterance]:
    """Merge utterance_N with utterance_N+1 if the pair clearly forms one
    sentence interrupted by Whisper's segmenter.

    Conditions (all must hold):
      * Same speaker (``speaker`` field equal).
      * utterance_N.text ends WITHOUT sentence-ending punctuation
        (``.`` ``?`` ``!`` ``…``). Ending on a comma / dash / colon /
        nothing all qualify.
      * utterance_N+1 starts with a lowercase letter, a digit, or one
        of the sentence-continuers (``і``, ``але``, ``що``, …).
    """
    result: list[Utterance] = []
    for utt in utterances:
        if not result:
            result.append(utt)
            continue

        prev = result[-1]
        if utt.speaker != prev.speaker:
            result.append(utt)
            continue
        if not _ends_mid_sentence(prev.text):
            result.append(utt)
            continue
        if not _starts_with_continuation(utt.text):
            result.append(utt)
            continue

        joined_text = f"{prev.text.rstrip()} {utt.text.lstrip()}"
        # Extend the previous utterance's time span to cover this one.
        joined = replace(
            prev,
            text=joined_text,
            end_sec=utt.end_sec if utt.end_sec is not None else prev.end_sec,
        )
        result[-1] = joined
    return result


def collapse_filler_runs(utterances: Sequence[Utterance]) -> list[Utterance]:
    """Collapse runs of 3+ identical short filler words WITHIN one
    utterance down to a single occurrence.

    We do NOT collapse across utterances (that would be a dedup problem,
    solved by :func:`dedup_exact`). The intent is to clean up cases like
    ``"так, так, так, дивіться"`` where Whisper locked on a repeat.

    "Short" = ≤ 4 letters. This captures typical UA fillers (``так``,
    ``ну``, ``ага``, ``да``) without touching substantive repeats.
    """
    result: list[Utterance] = []
    for utt in utterances:
        result.append(replace(utt, text=_collapse_in_text(utt.text)))
    return result


def _collapse_in_text(text: str) -> str:
    tokens = list(_tokenise_keep_whitespace(text))
    if len(tokens) < _FILLER_COLLAPSE_THRESHOLD * 2:
        return text

    i = 0
    out_parts: list[str] = []
    while i < len(tokens):
        kind, value = tokens[i]
        if kind != "word" or len(value) > 4:
            out_parts.append(value)
            i += 1
            continue

        # Peek ahead: count consecutive (word, same-lower, short) separated
        # only by whitespace/punctuation.
        run_end = i + 1
        run_count = 1
        while run_end < len(tokens):
            k, v = tokens[run_end]
            if k == "word":
                if v.casefold() != value.casefold():
                    break
                run_count += 1
            run_end += 1

        if run_count >= _FILLER_COLLAPSE_THRESHOLD:
            out_parts.append(value)
            # The run_end loop walked past any trailing comma/space that
            # was between the LAST matching filler and the next word.
            # Emit that separator verbatim so we don't concatenate the
            # collapsed filler to the following word ("тактак дивіться").
            trailing_sep = ""
            for back in range(run_end - 1, i, -1):
                kind_b, value_b = tokens[back]
                if kind_b == "sep":
                    trailing_sep = value_b
                    break
                # Keep walking back only past word tokens that are part of
                # this filler run (same-lower). Anything else means we ran
                # out of separator space.
                if kind_b == "word" and value_b.casefold() != value.casefold():
                    break
            if trailing_sep:
                # Strip leading commas so we end with just whitespace between
                # the collapsed filler and the next word.
                cleaned_sep = trailing_sep.lstrip(", ") or " "
                # Ensure at least one space so adjoining words don't merge.
                if not cleaned_sep or not cleaned_sep[0].isspace():
                    cleaned_sep = " " + cleaned_sep
                out_parts.append(cleaned_sep)
            else:
                out_parts.append(" ")
            i = run_end
        else:
            out_parts.append(value)
            i += 1
    return "".join(out_parts)


def _tokenise_keep_whitespace(text: str):
    """Yield (kind, value) tokens, preserving whitespace and punctuation.

    kind is "word" for word-like runs and "sep" otherwise.
    """
    pos = 0
    for match in _WORD_RE.finditer(text):
        start, end = match.span()
        if start > pos:
            yield ("sep", text[pos:start])
        yield ("word", text[start:end])
        pos = end
    if pos < len(text):
        yield ("sep", text[pos:])


# --- composite rule-based cleanup -------------------------------------------


def rule_based_cleanup(transcript: Transcript) -> Transcript:
    """Apply the full rule-based cleanup pipeline (A + C + join + filler)."""
    utterances = list(transcript.utterances)
    utterances = dedup_exact(utterances)
    utterances = merge_rolling_overlap(utterances)
    utterances = join_broken_sentences(utterances)
    utterances = collapse_filler_runs(utterances)
    return Transcript(
        utterances=tuple(utterances),
        detected_language=transcript.detected_language,
        detected_language_probability=transcript.detected_language_probability,
    )


# --- subsequence validator --------------------------------------------------


def is_word_subsequence(original: str, rewritten: str) -> bool:
    """True if every word-token in ``rewritten`` appears, in order, in
    ``original`` (case-insensitive).

    Whitespace, punctuation, and capitalisation are ignored. Skipping
    words in the original is allowed (a cleanup removes words), but
    adding any word to the rewritten version fails the check.
    """
    orig_words = _words_lower(original)
    new_words = _words_lower(rewritten)

    i = 0
    for word in new_words:
        while i < len(orig_words) and orig_words[i] != word:
            i += 1
        if i == len(orig_words):
            return False
        i += 1
    return True


# --- LLM cleanup (opt-in) ---------------------------------------------------


@dataclass(frozen=True)
class LLMCleanupResult:
    """Return value of :func:`llm_cleanup`.

    ``transcript`` is always safe to write: we fall back to the
    rule-based result if the LLM fails validation. ``used_llm`` reports
    whether the LLM output was actually kept.
    """

    transcript: Transcript
    used_llm: bool
    reason: str = ""


_CLEANUP_SYSTEM = (
    "You rewrite a speech-recognition transcript to remove duplicates, "
    "repeated chunks and broken sentence fragments. You NEVER paraphrase, "
    "NEVER add new information, NEVER translate, and you preserve the "
    "speaker's exact words, tone, and pronouns. If you are unsure whether "
    "something is a repeat, keep it as-is. Every word in your output must "
    "appear in the input. You respond ONLY with valid JSON."
)


def _cleanup_user_prompt(raw_text: str, language: Optional[str]) -> str:
    lang_line = (
        f"The transcript is in {language}. " if language else ""
    )
    return (
        lang_line
        + "Return a JSON object with EXACTLY one field:\n"
          '  - "text": the cleaned transcript as a single string.\n'
          "Rules:\n"
          "  1. Remove utterances that repeat a nearby utterance almost verbatim.\n"
          "  2. When the start of one line parrots the end of the previous line, "
          "keep only the new tail.\n"
          "  3. Do NOT rephrase or shorten sentences. Keep the speaker's exact words.\n"
          "  4. Do NOT introduce any word that is not already in the input.\n"
          "  5. Keep chapter headings, timecodes, and speaker labels unchanged.\n\n"
          "TRANSCRIPT:\n"
          + raw_text.strip()
    )


def llm_cleanup(
    transcript: Transcript,
    *,
    llm_client,
    language: Optional[str] = None,
    logger_fn: Optional[Callable[[str], None]] = None,
) -> LLMCleanupResult:
    """Run a conservative LLM-driven cleanup, validated by word-subsequence.

    Always starts from the rule-based result (so the LLM never has to
    handle trivial dedup), then asks the LLM to polish further. If the
    LLM adds a word that isn't in the rule-based text, we reject the
    LLM output and keep the rule-based transcript.
    """
    log = logger_fn or (lambda _msg: None)

    baseline = rule_based_cleanup(transcript)
    if llm_client is None:
        return LLMCleanupResult(
            transcript=baseline, used_llm=False, reason="no_client"
        )

    try:
        available = getattr(llm_client, "is_available", lambda: True)()
    except Exception:  # pragma: no cover - defensive
        available = False
    if not available:
        log("LLM cleanup: client not available; using rule-based result")
        return LLMCleanupResult(
            transcript=baseline, used_llm=False, reason="unavailable"
        )

    raw_text = _render_for_llm(baseline)
    try:
        data = llm_client.chat_json(
            system_prompt=_CLEANUP_SYSTEM,
            user_prompt=_cleanup_user_prompt(raw_text, language),
        )
    except Exception as exc:  # pragma: no cover - provider-dependent
        log(f"LLM cleanup: call failed ({exc}); using rule-based result")
        return LLMCleanupResult(
            transcript=baseline, used_llm=False, reason="call_failed"
        )

    cleaned_text = str(data.get("text", "")).strip()
    if not cleaned_text:
        log("LLM cleanup: empty text; using rule-based result")
        return LLMCleanupResult(
            transcript=baseline, used_llm=False, reason="empty"
        )

    if not is_word_subsequence(raw_text, cleaned_text):
        log(
            "LLM cleanup: added words not in the source; "
            "rejecting LLM output and keeping rule-based"
        )
        return LLMCleanupResult(
            transcript=baseline, used_llm=False, reason="not_subsequence"
        )

    # Re-materialise the cleaned text back as a Transcript using the
    # baseline timing info. We keep the utterance boundaries of the
    # baseline and redistribute the LLM text across them by scaling on
    # word count, which preserves rough timecodes for the writer.
    rebuilt = _rebuild_transcript_from_text(baseline, cleaned_text)
    return LLMCleanupResult(transcript=rebuilt, used_llm=True)


def _render_for_llm(transcript: Transcript) -> str:
    """Render a transcript as a line-per-utterance plain-text document.

    Timecodes are preserved so the LLM can keep them in place if it
    wishes. Speakers are dropped (our current pipeline has one speaker).
    """
    lines: list[str] = []
    for utt in transcript.utterances:
        timecode = _format_timecode_for_llm(utt.start_sec)
        prefix = f"{timecode} " if timecode else ""
        lines.append(f"{prefix}{utt.text.strip()}")
    return "\n".join(lines)


def _format_timecode_for_llm(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def _rebuild_transcript_from_text(
    baseline: Transcript, cleaned_text: str
) -> Transcript:
    """Distribute ``cleaned_text`` over ``baseline``'s utterances in
    proportion to each utterance's word count, so timecodes remain
    roughly aligned.

    Falls back to stuffing the entire cleaned string into the first
    utterance if the baseline has zero words.
    """
    cleaned_lines = [line.strip() for line in cleaned_text.splitlines() if line.strip()]
    utterances = list(baseline.utterances)

    if not utterances:
        return baseline

    # Strip any leading timecode prefix that the LLM preserved.
    tc_re = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*")
    lines = [tc_re.sub("", line) for line in cleaned_lines]

    # Map 1:1 if the line count matches exactly (common case — LLM kept
    # one line per utterance).
    if len(lines) == len(utterances):
        return Transcript(
            utterances=tuple(
                replace(utt, text=text) for utt, text in zip(utterances, lines)
            ),
            detected_language=baseline.detected_language,
            detected_language_probability=baseline.detected_language_probability,
        )

    # Otherwise, redistribute proportionally to utterance word counts.
    words_per_utt = [len(_words_lower(u.text)) or 1 for u in utterances]
    all_words = cleaned_text.split()
    total_weight = sum(words_per_utt)
    rebuilt: list[Utterance] = []
    cursor = 0
    for idx, utt in enumerate(utterances):
        remaining = len(all_words) - cursor
        if idx == len(utterances) - 1:
            slice_len = remaining
        else:
            slice_len = int(round(len(all_words) * words_per_utt[idx] / total_weight))
            slice_len = max(1, min(slice_len, remaining))
        new_text = " ".join(all_words[cursor : cursor + slice_len])
        cursor += slice_len
        rebuilt.append(replace(utt, text=new_text or utt.text))
    return Transcript(
        utterances=tuple(rebuilt),
        detected_language=baseline.detected_language,
        detected_language_probability=baseline.detected_language_probability,
    )


__all__ = [
    "CleanMode",
    "DUP_WINDOW_SEC",
    "SHINGLE_OVERLAP_THRESHOLD",
    "LLMCleanupResult",
    "collapse_filler_runs",
    "dedup_exact",
    "is_word_subsequence",
    "join_broken_sentences",
    "llm_cleanup",
    "merge_rolling_overlap",
    "rule_based_cleanup",
]
