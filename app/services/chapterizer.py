from __future__ import annotations

import re
from dataclasses import dataclass
from math import sqrt
from typing import Literal

from app.models.types import Transcript
from app.services.stopwords import ALL_STOPWORDS

TOKEN_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)*", flags=re.UNICODE)

ChapterTitleStyle = Literal["keywords", "snippet"]


@dataclass(frozen=True)
class Chapter:
    title: str
    start_index: int
    end_index: int
    start_sec: float | None


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]


_REPEAT_PHRASE_MIN_COUNT = 3
_REPEAT_PHRASE_MAX_LEN = 60


def _filter_repeated_phrases(texts: list[str]) -> list[str]:
    """Drop short utterances that repeat 3+ times inside the block.

    Whisper's trail-loop artefact (e.g. ``Here's the problem.`` emitted
    1800 times) leaks into per-chapter text and dominates tf-idf even
    after :func:`Transcriber._trim_trailing_repeat_loop` keeps only two
    copies. This filter removes those leaked copies before keyword
    extraction so they don't poison chapter titles.
    """
    counts: dict[str, int] = {}
    normalized: list[str] = []
    for text in texts:
        norm = re.sub(r"\s+", " ", text.strip().lower())
        normalized.append(norm)
        if len(norm) <= _REPEAT_PHRASE_MAX_LEN:
            counts[norm] = counts.get(norm, 0) + 1

    blocked = {
        phrase for phrase, count in counts.items() if count >= _REPEAT_PHRASE_MIN_COUNT
    }
    if not blocked:
        return texts

    return [text for text, norm in zip(texts, normalized) if norm not in blocked]


def _keywords(texts: list[str], top_n: int = 3) -> list[str]:
    counts: dict[str, int] = {}
    for text in texts:
        for token in _tokenize(text):
            if len(token) < 3 or token in ALL_STOPWORDS:
                continue
            counts[token] = counts.get(token, 0) + 1

    if not counts:
        return []

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _ in ranked[:top_n]]


def _term_freq(texts: list[str]) -> dict[str, float]:
    tf: dict[str, float] = {}
    total = 0
    for text in texts:
        for token in _tokenize(text):
            if len(token) < 3 or token in ALL_STOPWORDS:
                continue
            tf[token] = tf.get(token, 0.0) + 1.0
            total += 1

    if total == 0:
        return {}

    for key in list(tf.keys()):
        tf[key] = tf[key] / total
    return tf


def _cosine_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0

    dot = 0.0
    for token, left_value in left.items():
        right_value = right.get(token)
        if right_value is None:
            continue
        dot += left_value * right_value

    left_norm = sqrt(sum(value * value for value in left.values()))
    right_norm = sqrt(sum(value * value for value in right.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0

    return dot / (left_norm * right_norm)


def _format_timecode(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def format_timecode_bracketed(seconds: float | None) -> str:
    """Public helper used by the summary writer: ``[HH:MM:SS]`` / ``[MM:SS]``."""
    if seconds is None:
        return ""
    return f"[{_format_timecode(seconds)}]"


def _first_meaningful_snippet(texts: list[str], max_words: int = 7) -> str:
    words: list[str] = []
    for text in texts:
        for token in _tokenize(text):
            if len(token) < 3 or token in ALL_STOPWORDS:
                continue
            words.append(token)
            if len(words) >= max_words:
                break
        if len(words) >= max_words:
            break
    if not words:
        return ""
    return " ".join(word.capitalize() for word in words)


def _chapter_title(
    texts: list[str],
    index: int,
    chapter_start_sec: float | None,
    style: ChapterTitleStyle = "keywords",
) -> str:
    prefix = (
        f"[{_format_timecode(chapter_start_sec)}] "
        if chapter_start_sec is not None
        else ""
    )
    chapter_prefix = f"Chapter {index:02d}: "

    filtered_texts = _filter_repeated_phrases(texts)
    if not filtered_texts:
        filtered_texts = texts

    if style == "snippet":
        body = _first_meaningful_snippet(filtered_texts)
        if not body:
            body = "Topic"
        return f"{prefix}{chapter_prefix}{body}"

    words = _keywords(texts=filtered_texts, top_n=3)
    if not words:
        return f"{prefix}{chapter_prefix}Topic"
    title_body = " / ".join(word.capitalize() for word in words)
    return f"{prefix}{chapter_prefix}{title_body}"


def _dedupe_titles(chapters: list[Chapter]) -> list[Chapter]:
    seen: dict[str, int] = {}
    result: list[Chapter] = []
    for chapter in chapters:
        key = chapter.title.casefold()
        count = seen.get(key, 0) + 1
        seen[key] = count
        if count == 1:
            result.append(chapter)
            continue

        result.append(
            Chapter(
                title=f"{chapter.title} (Part {count})",
                start_index=chapter.start_index,
                end_index=chapter.end_index,
                start_sec=chapter.start_sec,
            )
        )
    return result


def _resolve_block_sizes(
    utterances_text: list[str],
    min_words: int,
    target_words: int,
    max_words: int,
) -> tuple[int, int, int]:
    total_utterances = len(utterances_text)
    if total_utterances == 0:
        return (8, 12, 18)

    total_words = 0
    for text in utterances_text:
        total_words += len(_tokenize(text))

    avg_words = total_words / total_utterances if total_words > 0 else 6.0
    avg_words = max(1.0, avg_words)

    min_size = int(round(min_words / avg_words))
    target_size = int(round(target_words / avg_words))
    max_size = int(round(max_words / avg_words))

    min_size = max(8, min(min_size, 80))
    target_size = max(min_size + 2, min(target_size, 120))
    max_size = max(target_size + 4, min(max_size, 160))
    return (min_size, target_size, max_size)


def build_chapters(
    transcript: Transcript,
    target_size: int | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    target_words: int = 400,
    min_words: int = 250,
    max_words: int = 700,
    context_window: int = 4,
    title_style: ChapterTitleStyle = "keywords",
) -> list[Chapter]:
    utterances = transcript.utterances
    total = len(utterances)
    if total == 0:
        return []

    resolved_min, resolved_target, resolved_max = _resolve_block_sizes(
        utterances_text=[u.text for u in utterances],
        min_words=min_words,
        target_words=target_words,
        max_words=max_words,
    )
    min_size = min_size if min_size is not None else resolved_min
    target_size = target_size if target_size is not None else resolved_target
    max_size = max_size if max_size is not None else resolved_max

    if min_size < 2:
        min_size = 2
    if target_size < min_size:
        target_size = min_size
    if max_size < target_size:
        max_size = target_size

    if total <= max(min_size + 2, target_size):
        first_start = utterances[0].start_sec
        return [
            Chapter(
                title=_chapter_title(
                    [u.text for u in utterances], 1, first_start, style=title_style
                ),
                start_index=0,
                end_index=total,
                start_sec=first_start,
            )
        ]

    boundaries = [0]
    cursor = 0
    while cursor < total:
        floor = cursor + min_size
        if floor >= total:
            break

        preferred_start = max(floor, cursor + target_size - 2)
        preferred_end = min(total - 1, cursor + max_size)
        if preferred_start > preferred_end:
            preferred_start = floor
            preferred_end = min(total - 1, floor)

        best_index = preferred_start
        best_score = float("inf")
        for boundary in range(preferred_start, preferred_end + 1):
            left_start = max(cursor, boundary - context_window)
            right_end = min(total, boundary + context_window)

            left_texts = [u.text for u in utterances[left_start:boundary]]
            right_texts = [u.text for u in utterances[boundary:right_end]]
            if not left_texts or not right_texts:
                continue

            score = _cosine_similarity(_term_freq(left_texts), _term_freq(right_texts))
            if score < best_score:
                best_score = score
                best_index = boundary

        boundaries.append(best_index)
        cursor = best_index

    if boundaries[-1] != total:
        boundaries.append(total)

    chapters: list[Chapter] = []
    for chapter_number, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:]), start=1):
        if start >= end:
            continue

        chapter_utterances = utterances[start:end]
        chapter_start = chapter_utterances[0].start_sec
        chapters.append(
            Chapter(
                title=_chapter_title(
                    [u.text for u in chapter_utterances],
                    chapter_number,
                    chapter_start,
                    style=title_style,
                ),
                start_index=start,
                end_index=end,
                start_sec=chapter_start,
            )
        )

    return _dedupe_titles(chapters)
