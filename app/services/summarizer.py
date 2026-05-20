"""Summarization service producing a four-section summary.

The generated summary file has:

1. **Overview** — a 2-4 sentence TL;DR of the video.
2. **Key Facts** — bullet list of concrete facts that were *explicitly
   spoken* (dates, numbers, names, prices, quantities). Each bullet can
   optionally end with a ``[MM:SS]`` timecode pointing back to the
   supporting utterance.
3. **Intents & Actions** — bullet list of actions/recommendations/
   predictions the speaker made: who, what, when.
4. **Per Chapter** — one short bullet per chapter.

Two strategies:

* :class:`ExtractiveSummarizer` — offline fallback. Uses frequency-based
  sentence scoring. Does its best to populate facts/intents by picking
  sentences containing digits, dates, or imperative verbs, but this is
  clearly inferior to the LLM path.
* :class:`LLMStructuredSummarizer` — sends the whole transcript plus
  per-chapter segments to Ollama with ``format: "json"`` and the schema
  ``{"overview": "...", "key_facts": [...], "intents": [...],
  "per_chapter": [{"chapter_index": 1, "title": "...", "bullet": "..."}]}``.
  The model is instructed NEVER to invent facts.

The :class:`Summarizer` façade tries the LLM first when requested and
falls back to extractive on failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Protocol, Sequence

from app.models.types import (
    ModelNotFoundError,
    ProviderUnavailableError,
    SummarizationError,
    SummarizationTimeoutError,
    SummaryOptions,
    Transcript,
    Utterance,
)
from app.services.stopwords import ALL_STOPWORDS

TOKEN_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)*", flags=re.UNICODE)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.\!\?…])\s+(?=[\w\[])", flags=re.UNICODE)
_DIGIT_RE = re.compile(r"\d")
_IMPERATIVE_MARKERS = (
    "має", "мають", "потрібно", "треба", "буде", "будуть",
    "обіцяв", "пропонує", "планує", "планують", "хоче", "хочуть",
    "збирається", "збираються", "прогнозує", "запустить",
)


@dataclass(frozen=True)
class Fact:
    text: str
    timecode: Optional[str] = None


@dataclass(frozen=True)
class Intent:
    text: str
    timecode: Optional[str] = None


@dataclass(frozen=True)
class ChapterSummary:
    chapter_index: int
    refined_title: str
    summary: str  # short bullet-ready sentence for the per-chapter section


@dataclass(frozen=True)
class SummaryResult:
    overview: str
    key_facts: tuple[Fact, ...] = field(default_factory=tuple)
    intents: tuple[Intent, ...] = field(default_factory=tuple)
    per_chapter: tuple[ChapterSummary, ...] = field(default_factory=tuple)


# --- tiny text utilities ----------------------------------------------------


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]


def _split_sentences(text: str) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    return [piece.strip() for piece in SENTENCE_SPLIT_RE.split(cleaned) if piece.strip()]


def _score_sentence(sentence: str, token_weights: dict[str, float]) -> float:
    tokens = _tokenize(sentence)
    if not tokens:
        return 0.0
    score = 0.0
    meaningful = 0
    for token in tokens:
        if len(token) < 3 or token in ALL_STOPWORDS:
            continue
        meaningful += 1
        score += token_weights.get(token, 0.0)
    if meaningful == 0:
        return 0.0
    return score / (meaningful ** 0.5)


def _weight_tokens(sentences: Sequence[str]) -> dict[str, float]:
    counts: dict[str, int] = {}
    for sentence in sentences:
        seen_in_sentence: set[str] = set()
        for token in _tokenize(sentence):
            if len(token) < 3 or token in ALL_STOPWORDS:
                continue
            if token in seen_in_sentence:
                continue
            seen_in_sentence.add(token)
            counts[token] = counts.get(token, 0) + 1
    if not counts:
        return {}
    total = sum(counts.values())
    return {token: count / total for token, count in counts.items()}


def _keywords(text: str, top_n: int = 6) -> list[str]:
    counts: dict[str, int] = {}
    for token in _tokenize(text):
        if len(token) < 4 or token in ALL_STOPWORDS:
            continue
        counts[token] = counts.get(token, 0) + 1
    if not counts:
        return []
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _ in ranked[:top_n]]


_NEAR_FACT_TIME_WINDOW_SEC = 2.5
_NEAR_FACT_OVERLAP_THRESHOLD = 0.4


def _meaningful_token_set(sentence: str) -> set[str]:
    return {
        token
        for token in _tokenize(sentence)
        if len(token) >= 3 and token not in ALL_STOPWORDS
    }


def _near_fact_duplicate(
    sentence: str,
    start_sec: Optional[float],
    kept: Sequence[tuple["Fact", Optional[float], set[str]]],
) -> bool:
    if start_sec is None or not kept:
        return False
    candidate_tokens = _meaningful_token_set(sentence)
    if not candidate_tokens:
        return False
    for _fact, prev_sec, prev_tokens in kept:
        if prev_sec is None:
            continue
        if abs(start_sec - prev_sec) > _NEAR_FACT_TIME_WINDOW_SEC:
            continue
        if not prev_tokens:
            continue
        overlap = len(candidate_tokens & prev_tokens)
        smaller = min(len(candidate_tokens), len(prev_tokens))
        if smaller == 0:
            continue
        if overlap / smaller >= _NEAR_FACT_OVERLAP_THRESHOLD:
            return True
    return False


def _format_timecode(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


# --- extractive path --------------------------------------------------------


class ExtractiveSummarizer:
    """Deterministic extractive summarizer. No network, no LLM."""

    def summarize_block(self, text: str, max_sentences: int = 3) -> str:
        if max_sentences <= 0:
            return ""
        sentences = _split_sentences(text)
        if not sentences:
            return ""
        if len(sentences) <= max_sentences:
            return " ".join(sentences)
        weights = _weight_tokens(sentences)
        scored = [
            (index, sentence, _score_sentence(sentence, weights))
            for index, sentence in enumerate(sentences)
        ]
        top = sorted(scored, key=lambda item: item[2], reverse=True)[:max_sentences]
        top_sorted = sorted(top, key=lambda item: item[0])
        return " ".join(sentence for _idx, sentence, _score in top_sorted)

    def derive_title(self, text: str, max_words: int = 7) -> str:
        words = _keywords(text, top_n=max_words)
        if not words:
            return "Topic"
        return " ".join(word.capitalize() for word in words)

    def extract_facts(
        self,
        utterances: Sequence[Utterance],
        *,
        max_facts: int = 8,
    ) -> list[Fact]:
        """Pick sentences that most likely contain hard facts (digits).

        Deduplication is two-tiered: (1) exact normalized-text match, and
        (2) near-timecode-and-overlap — if a candidate shares a timecode
        (within ~2 seconds) with a kept fact AND their meaningful tokens
        overlap by >=40%, it's treated as the same fact. This collapses
        fragmented micro-sentences like ``They wanted to pay 60%.`` and
        ``We would pay 40%.`` when Whisper splits one thought into two
        adjacent utterances.
        """
        candidates: list[tuple[str, Optional[str], Optional[float]]] = []
        for utt in utterances:
            text = utt.text
            timecode = _format_timecode(utt.start_sec)
            for sentence in _split_sentences(text):
                if _DIGIT_RE.search(sentence):
                    candidates.append((sentence, timecode, utt.start_sec))
                    if len(candidates) >= max_facts * 3:
                        break
            if len(candidates) >= max_facts * 3:
                break

        seen_exact: set[str] = set()
        kept: list[tuple[Fact, Optional[float], set[str]]] = []
        for sentence, timecode, start_sec in candidates:
            key = re.sub(r"\s+", " ", sentence.lower())
            if key in seen_exact:
                continue
            if _near_fact_duplicate(sentence, start_sec, kept):
                continue
            seen_exact.add(key)
            tokens = _meaningful_token_set(sentence)
            kept.append((Fact(text=sentence.strip(), timecode=timecode), start_sec, tokens))
            if len(kept) >= max_facts:
                break
        return [fact for fact, _sec, _tokens in kept]

    def extract_intents(
        self,
        utterances: Sequence[Utterance],
        *,
        max_intents: int = 5,
    ) -> list[Intent]:
        """Pick sentences containing imperative/future-tense markers."""
        candidates: list[tuple[str, Optional[str]]] = []
        for utt in utterances:
            text_lower = utt.text.lower()
            if not any(marker in text_lower for marker in _IMPERATIVE_MARKERS):
                continue
            timecode = _format_timecode(utt.start_sec)
            for sentence in _split_sentences(utt.text):
                lower = sentence.lower()
                if any(marker in lower for marker in _IMPERATIVE_MARKERS):
                    candidates.append((sentence, timecode))

        seen: set[str] = set()
        result: list[Intent] = []
        for sentence, timecode in candidates:
            key = re.sub(r"\s+", " ", sentence.lower())
            if key in seen:
                continue
            seen.add(key)
            result.append(Intent(text=sentence.strip(), timecode=timecode))
            if len(result) >= max_intents:
                break
        return result


# --- LLM path ---------------------------------------------------------------


class _LLMClientProtocol(Protocol):
    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        response_schema: Optional[dict] = None,
    ) -> dict: ...

    def is_available(self) -> bool: ...


_SYSTEM_PROMPT = (
    "You summarise video transcripts. You NEVER invent facts. You quote "
    "numbers, dates, and proper names exactly as they appear in the "
    "transcript. You reply in the SAME language as the transcript. You "
    "respond ONLY with valid JSON that matches the user's schema."
)


_CHAPTER_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "bullet": {"type": "string"},
    },
    "required": ["title", "bullet"],
}

_SYNTHESIS_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "key_facts": {"type": "array", "items": {"type": "string"}},
        "intents": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overview", "key_facts", "intents"],
}

_CHAPTER_BULLET_PROMPT_TEMPLATE = (
    "{lang}"
    "Return a JSON object with EXACTLY these fields for this chapter:\n"
    '  - "title": a descriptive title, {max_words} words or fewer, no '
    "quotes, no trailing period.\n"
    '  - "bullet": a single concise sentence describing what this '
    "chapter is about. Do NOT paraphrase opinions as facts.\n"
    "CHAPTER TRANSCRIPT:\n{text}"
)


_SYNTHESIS_PROMPT_TEMPLATE = (
    "{lang}"
    "Return a JSON object with EXACTLY these fields:\n"
    '  - "overview": a {overview} sentence prose summary of the whole '
    "video.\n"
    '  - "key_facts": an array of up to {max_facts} short strings, each '
    "a specific fact explicitly stated in the transcript (numbers, "
    "dates, names, prices, quantities). Quote numbers exactly. Do NOT "
    "include speculation or opinion. Prefix each fact with its "
    "timecode in square brackets if known, e.g. \"[02:13] $2M per "
    "passage\".\n"
    '  - "intents": an array of up to {max_intents} short strings '
    "describing concrete actions, recommendations or predictions the "
    "speaker made. Same timecode-prefix convention.\n"
    "Do NOT include any extra keys.\n\n"
    "CHAPTER SUMMARIES (use as structure):\n{chapter_block}\n\n"
    "FULL TRANSCRIPT (ground truth — extract facts from here):\n{full_text}"
)


class LLMStructuredSummarizer:
    """Runs per-chapter + full-video summarization through an LLM."""

    def __init__(
        self,
        client: _LLMClientProtocol,
        *,
        overview_sentences: int = 4,
        title_max_words: int = 7,
        max_facts: int = 8,
        max_intents: int = 5,
    ) -> None:
        self.client = client
        self.overview_sentences = overview_sentences
        self.title_max_words = title_max_words
        self.max_facts = max_facts
        self.max_intents = max_intents

    def summarize_chapter(
        self,
        chapter_text: str,
        chapter_index: int,
        language: Optional[str],
    ) -> ChapterSummary:
        lang_line = (
            f"Reply in {language}.\n"
            if language
            else "Reply in the SAME language as the transcript below.\n"
        )
        user = _CHAPTER_BULLET_PROMPT_TEMPLATE.format(
            lang=lang_line,
            max_words=self.title_max_words,
            text=chapter_text.strip(),
        )
        data = self.client.chat_json(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user,
            response_schema=_CHAPTER_JSON_SCHEMA,
        )
        title = _normalize_title(
            str(data.get("title", "")).strip(), max_words=self.title_max_words
        )
        bullet = str(data.get("bullet", "")).strip()
        if not title:
            raise SummarizationError("LLM returned empty chapter title")
        if not bullet:
            raise SummarizationError("LLM returned empty chapter bullet")
        return ChapterSummary(
            chapter_index=chapter_index,
            refined_title=title,
            summary=bullet,
        )

    def synthesize(
        self,
        *,
        per_chapter: Sequence[ChapterSummary],
        full_text: str,
        language: Optional[str],
    ) -> tuple[str, list[Fact], list[Intent]]:
        lang_line = (
            f"Reply in {language}.\n"
            if language
            else "Reply in the SAME language as the transcript below.\n"
        )
        chapter_block = "\n".join(
            f"{cs.chapter_index}. {cs.refined_title} — {cs.summary}"
            for cs in per_chapter
        )
        user = _SYNTHESIS_PROMPT_TEMPLATE.format(
            lang=lang_line,
            overview=self.overview_sentences,
            max_facts=self.max_facts,
            max_intents=self.max_intents,
            chapter_block=chapter_block,
            full_text=full_text.strip(),
        )
        data = self.client.chat_json(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user,
            response_schema=_SYNTHESIS_JSON_SCHEMA,
        )
        overview = str(data.get("overview", "")).strip()
        facts = _parse_fact_list(data.get("key_facts"))
        intents = _parse_intent_list(data.get("intents"))
        return overview, facts, intents


def _parse_fact_list(raw) -> list[Fact]:
    return [Fact(text=text, timecode=tc) for text, tc in _iter_timecoded(raw)]


def _parse_intent_list(raw) -> list[Intent]:
    return [Intent(text=text, timecode=tc) for text, tc in _iter_timecoded(raw)]


_LEADING_TC_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*")


def _iter_timecoded(raw) -> Iterable[tuple[str, Optional[str]]]:
    if not raw:
        return []
    items: list[tuple[str, Optional[str]]] = []
    for entry in raw:
        if isinstance(entry, str):
            text = entry.strip()
            if not text:
                continue
            match = _LEADING_TC_RE.match(text)
            if match:
                items.append((text[match.end():].strip(), f"[{match.group(1)}]"))
            else:
                items.append((text, None))
        elif isinstance(entry, dict):
            text = str(entry.get("text") or entry.get("fact") or entry.get("intent") or "").strip()
            if not text:
                continue
            tc = entry.get("timecode")
            items.append((text, str(tc) if tc else None))
    return items


def _normalize_title(title: str, max_words: int) -> str:
    cleaned = re.sub(r"\s+", " ", title).strip()
    cleaned = cleaned.strip("#*_` \"'").strip()
    cleaned = re.sub(r"[.\s]+$", "", cleaned)
    words = cleaned.split()
    if len(words) > max_words:
        cleaned = " ".join(words[:max_words])
    return cleaned


def _utterances_to_plain_text(utterances: Sequence[Utterance]) -> str:
    return " ".join(u.text.strip() for u in utterances if u.text.strip())


def _utterances_with_timecodes(utterances: Sequence[Utterance]) -> str:
    lines: list[str] = []
    for utt in utterances:
        timecode = _format_timecode(utt.start_sec)
        prefix = f"{timecode} " if timecode else ""
        text = utt.text.strip()
        if text:
            lines.append(f"{prefix}{text}")
    return "\n".join(lines)


# --- façade -----------------------------------------------------------------


class Summarizer:
    """Entry point used by the CLI and MCP server."""

    def __init__(
        self,
        options: SummaryOptions,
        llm_client: Optional[_LLMClientProtocol] = None,
        logger_fn=None,
    ) -> None:
        self.options = options
        self._llm = (
            LLMStructuredSummarizer(
                client=llm_client,
                overview_sentences=options.overview_sentences,
                title_max_words=options.title_max_words,
            )
            if llm_client is not None
            else None
        )
        self._extractive = ExtractiveSummarizer()
        self._logger = logger_fn or (lambda message: None)

    def summarize(
        self,
        transcript: Transcript,
        chapters: Sequence[object],
        language: Optional[str] = None,
    ) -> SummaryResult:
        if self.options.mode == "none" or not transcript.utterances:
            return SummaryResult(overview="", per_chapter=())

        per_chapter_out: list[ChapterSummary] = []
        chapter_texts: list[str] = []

        for index, chapter in enumerate(chapters, start=1):
            chapter_utterances = transcript.utterances[chapter.start_index:chapter.end_index]
            chapter_text = _utterances_to_plain_text(chapter_utterances)
            chapter_texts.append(chapter_text)
            per_chapter_out.append(self._one_chapter(index, chapter_text, language))

        full_transcript_text = _utterances_with_timecodes(transcript.utterances)

        overview, key_facts, intents = self._synthesize(
            per_chapter=per_chapter_out,
            full_text=full_transcript_text,
            chapter_texts=chapter_texts,
            all_utterances=transcript.utterances,
            language=language,
        )

        return SummaryResult(
            overview=overview,
            key_facts=tuple(key_facts),
            intents=tuple(intents),
            per_chapter=tuple(per_chapter_out),
        )

    # ---- Internals ---------------------------------------------------------

    def _one_chapter(
        self, index: int, chapter_text: str, language: Optional[str]
    ) -> ChapterSummary:
        if self._llm is not None:
            try:
                return self._llm.summarize_chapter(
                    chapter_text=chapter_text,
                    chapter_index=index,
                    language=language,
                )
            except (
                ModelNotFoundError,
                ProviderUnavailableError,
                SummarizationTimeoutError,
                SummarizationError,
            ) as exc:
                self._logger(
                    f"LLM summary failed for chapter {index}, using extractive: {exc}"
                )
        # Extractive fallback: first-non-empty or extractive top-1.
        title = self._extractive.derive_title(
            chapter_text, max_words=self.options.title_max_words
        )
        summary = self._extractive.summarize_block(chapter_text, max_sentences=1)
        return ChapterSummary(
            chapter_index=index,
            refined_title=title,
            summary=summary,
        )

    def _synthesize(
        self,
        *,
        per_chapter: Sequence[ChapterSummary],
        full_text: str,
        chapter_texts: Sequence[str],
        all_utterances: Sequence[Utterance],
        language: Optional[str],
    ) -> tuple[str, list[Fact], list[Intent]]:
        if self._llm is not None:
            try:
                return self._llm.synthesize(
                    per_chapter=per_chapter,
                    full_text=full_text,
                    language=language,
                )
            except (
                ModelNotFoundError,
                ProviderUnavailableError,
                SummarizationTimeoutError,
                SummarizationError,
            ) as exc:
                self._logger(
                    f"LLM synthesis failed, using extractive: {exc}"
                )
        # Extractive fallback.
        joined_chapter_text = " ".join(chapter_texts)
        overview = self._extractive.summarize_block(
            joined_chapter_text, max_sentences=self.options.overview_sentences
        )
        facts = self._extractive.extract_facts(all_utterances)
        intents = self._extractive.extract_intents(all_utterances)
        return overview, facts, intents
