"""Summarization service.

A transcript becomes a titled digest, all driven by the SAME summary model:

1. Split the transcript into chunks of ~``_CHUNK_TARGET_TOKENS`` tokens,
   each ending on a sentence boundary (see app.services.chunking). The
   chunks DO NOT overlap.
2. Summarise each chunk STANDALONE — the model sees ONLY its own chunk
   (minimal prompt, see ``_build_minimal_prompts``), with a foreign-script
   guard that retries / strips CJK or Russian drift.
3. Give each chunk summary a short title and emit it as a "## title + body"
   block (short bodies below ``_TITLE_MIN_CHARS`` get no heading).
4. Concatenate the titled blocks with a blank-line seam into the final
   digest, which we hand straight to the writer.

A transcript that fits in one chunk skips the per-chunk loop and produces a
single titled block.

NOTE: two further stages exist in the codebase but are currently DISABLED
(kept commented in ``summarize`` for A/B): a fold-merge seam stitch
(``app.services.seam_stitch``) and a publicistic formatter
(``app.services.formatter``). The standalone-chunk → concatenate path above
is what actually ships.

Every call uses the same instructions, localised to the transcript's
language so the model never translates the summary. The model returns
ready-to-write markdown which we hand straight to the writer — no
parsing, no entity checklist, no refine pass.

If the LLM is unavailable (Ollama down, model OOM, request timeout) the
summarizer raises. The pipeline catches the failure and proceeds
without a summary file; the transcription artefact is independent and
still gets written.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

from app.models.types import (
    ProviderUnavailableError,
    SummarizationError,
    SummaryOptions,
    Transcript,
)
import os
import re

from app.services.chunking import chunk_text

# NOTE: ``app.services.seam_stitch.stitch_summaries`` (stage 2) and
# ``app.services.formatter.format_digest`` (stage 3) are NOT imported here
# because the production path currently concatenates standalone chunk
# summaries instead (see ``summarize`` below). Both modules are retained in
# the codebase for A/B comparison; re-add the imports if those stages are
# turned back on.


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# Target token budget per chunk fed to the LLM. With the prev-context
# technique the model sees TWO chunks per call (previous as context-only +
# current), so a 450-token chunk means ~900 input tokens — inside the
# range where qwen2.5:3b holds every story without dropping any (~1.5k is
# the reliable ceiling). Bigger chunks edged toward that ceiling and let
# the stitch bloat; 450 was the validated sweet spot across news and
# non-news transcripts. Overridable via env for tuning sweeps.
_CHUNK_TARGET_TOKENS = _env_int("DESCRIBELY_CHUNK_TOKENS", 450)

# Overlap between adjacent chunks. ZERO by design: the prev-context
# technique does not bake the previous chunk's tail INTO the next chunk
# (that duplicated boundary stories and forced the stitch to dedup).
# Instead the whole previous chunk is passed as separate context-only
# input, so each chunk's text is summarised exactly once.
_CHUNK_OVERLAP_TOKENS = _env_int("DESCRIBELY_CHUNK_OVERLAP", 0)

# Minimum chunk size. The effective target is clamped up to this, so it
# must not exceed the target or it would silently override it. With
# prev-context a 450-token chunk is never context-starved — the previous
# chunk travels with it — so the old 800 floor (a guard for the tail
# technique, where a lone tiny shard had no surrounding context and a 7B
# invented a city for it) is no longer needed and would clamp 450 → 800.
# Env-overridable so the eval harness can deliberately probe tiny chunks.
_MIN_CHUNK_TOKENS = _env_int("DESCRIBELY_MIN_CHUNK_TOKENS", 450)

# Stage-3 titles are skipped for chunk summaries shorter than this many
# characters — a 1-2 sentence body doesn't need a heading. Env-overridable.
_TITLE_MIN_CHARS = _env_int("DESCRIBELY_TITLE_MIN_CHARS", 200)

# Foreign-script guard. qwen2.5 sometimes drifts mid-generation into Chinese
# (and, on Ukrainian input, into Russian) despite the prompt's language
# anchor. We DETECT that in the output and retry the call; CJK that survives
# retries is stripped as a last resort so no hieroglyphs ever ship. Russian
# drift can't be auto-corrected (shared Cyrillic alphabet) but the
# Ukrainian-illegal letters ы/ъ/э/ё are a reliable signal to retry.
_CJK_RE = re.compile(
    "[぀-ヿ㐀-䶿一-鿿가-힯ｦ-ﾟ]"
)
_RU_ONLY_RE = re.compile("[ыъэёЫЪЭЁ]")


def _contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _strip_cjk(text: str) -> str:
    """Remove CJK characters and tidy the whitespace they leave behind."""
    cleaned = _CJK_RE.sub("", text or "")
    # Collapse spaces and stray punctuation left dangling by the removal.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned


def _is_foreign_output(text: str, language: Optional[str]) -> bool:
    """True if ``text`` drifted out of the transcript's script/language."""
    if _contains_cjk(text):
        return True
    code = (language or "").strip().lower()
    if code in ("uk", "ukrainian") and _RU_ONLY_RE.search(text or ""):
        return True
    return False

# Reports summarization progress as a fraction in [0, 1], mirroring the
# transcription progress callback so the GUI can drive an identical bar.
ProgressCallback = Callable[[float], None]


def _emit(callback: Optional[ProgressCallback], fraction: float) -> None:
    if callback is not None:
        callback(max(0.0, min(1.0, fraction)))


def _dump_chunks_if_requested(chunks, partials, logger) -> None:
    """Write the RAW per-chunk stage-1 summaries (pre-stitch) to a file.

    Enabled by setting ``DESCRIBELY_DUMP_CHUNKS`` to a path. Each block shows
    the input transcript chunk and the stage-1 summary the model produced for
    it, in order, with NO fold-merge stitch applied — a diagnostic view of
    where language drift / fabrication first appears (stage 1 vs stage 2).
    """
    path = os.environ.get("DESCRIBELY_DUMP_CHUNKS")
    if not path:
        return
    try:
        from pathlib import Path

        n = len(partials)
        blocks: list[str] = []
        for i in range(n):
            src = chunks[i].strip() if i < len(chunks) else ""
            blocks.append(f"===== CHUNK {i + 1}/{n} =====")
            blocks.append("--- ВХІД (сирий фрагмент транскрипту) ---")
            blocks.append(src)
            blocks.append("--- STAGE-1 SUMMARY (без склейки) ---")
            blocks.append((partials[i] or "").strip())
            blocks.append("")
        Path(path).write_text("\n".join(blocks), encoding="utf-8")
        logger(f"[summarize] wrote {n} raw chunk summaries -> {path}")
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break a run
        logger(f"[summarize] chunk dump failed: {exc}")


# --- LLM client protocol ---------------------------------------------------


class _LLMClientProtocol(Protocol):
    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.0,
        num_predict_floor: Optional[int] = None,
    ) -> str: ...

    def is_available(self) -> bool: ...


# --- Language handling -----------------------------------------------------
#
# The LLM gets instructions in the *same* language as the transcript so it
# isn't tempted to translate the summary. For Ukrainian and English we
# carry full prompt templates; for any other detected language we use the
# English prompt and prefix it with an explicit "Transcript is in {X}"
# directive so the model knows to write the output in that language.

# ISO-639-1 code (or English alias) → human-readable English name.
# Whisper returns lowercase codes; we accept both for robustness.
_LANGUAGE_NAMES: dict[str, str] = {
    "uk": "Ukrainian",
    "ukrainian": "Ukrainian",
    "ru": "Russian",
    "russian": "Russian",
    "en": "English",
    "english": "English",
    "pl": "Polish",
    "polish": "Polish",
    "de": "German",
    "german": "German",
    "fr": "French",
    "french": "French",
    "es": "Spanish",
    "spanish": "Spanish",
    "it": "Italian",
    "italian": "Italian",
    "pt": "Portuguese",
    "portuguese": "Portuguese",
    "cs": "Czech",
    "czech": "Czech",
    "sk": "Slovak",
    "slovak": "Slovak",
    "be": "Belarusian",
    "belarusian": "Belarusian",
    "tr": "Turkish",
    "turkish": "Turkish",
    "nl": "Dutch",
    "dutch": "Dutch",
    "ro": "Romanian",
    "romanian": "Romanian",
    "ja": "Japanese",
    "japanese": "Japanese",
    "zh": "Chinese",
    "chinese": "Chinese",
}


def _resolve_language_name(language: Optional[str]) -> Optional[str]:
    if not language:
        return None
    key = str(language).strip().lower()
    if not key:
        return None
    return _LANGUAGE_NAMES.get(key)


# --- Prompts ---------------------------------------------------------------
#
# Two native templates (uk, en) cover ~99% of our content; everything else
# falls back to the English template with a language directive prepended.
# Instructions repeat the "never invent facts" guard rail because LLMs at
# the 7-8B parameter range do listen to it more reliably when it's stated
# both in the system prompt AND the user prompt.

_SYSTEM_PROMPT_UK = (
    "Ви — асистент, що готує наративний дайджест транскриптів відео "
    "будь-якого типу (новини, лекція, туторіал, огляд, інтерв'ю, влог, "
    "розмова). Ви викладаєте матеріал суцільним текстом з абзаців — не "
    "списками, не таблицями, без рубрик. Ви НІКОЛИ не вигадуєте факти і "
    "НЕ додаєте того, чого немає в тексті: імена, числа, дати, локації, "
    "ціни і терміни передаєте дослівно, а якщо їх у транскрипті немає — "
    "не згадуєте взагалі. Довжина переказу відповідає РЕАЛЬНОМУ обсягу "
    "змісту: фрагмент з одного речення дає щонайбільше односеннєвий "
    "переказ. Ви НІКОЛИ не доповнюєте бідний фрагмент — не припускаєте, "
    "що мовець «ймовірно» розкриває чи «далі обговорює», не продовжуєте "
    "сюжет поза текстом, не домислюєте контекст, наслідки чи деталі. "
    "Якщо у фрагменті майже нічого немає — ваша відповідь майже порожня, "
    "і це правильно, а не помилка. Ви відповідаєте тією ж мовою, що й транскрипт, "
    "у форматі готового markdown — без передмов, без коментарів про себе."
)

# NOTE: the format rules live AFTER {transcript}, not before it. Ollama
# truncates an over-length prompt by dropping tokens from the START of the
# context window. Keeping the rules in the trailing block guarantees the
# model always sees them. See the num_ctx comment in ollama_provider.py.
_USER_PROMPT_UK = (
    "Нижче поданий транскрипт відео. Підготуйте з нього наративний "
    "дайджест за інструкціями ПІСЛЯ транскрипту.\n\n"
    "ТРАНСКРИПТ:\n{transcript}\n\n"
    "---\n"
    "ЗАВДАННЯ: пройдіть текст від початку до кінця і опишіть ПОСЛІДОВНО "
    "КОЖНУ окрему тему, думку, сюжет чи крок міркування окремим абзацом — "
    "стільки абзаців, скільки окремих тем у тексті. Відео може бути яким "
    "завгодно: новини, лекція, туторіал, огляд, інтерв'ю, влог, розмова, "
    "демонстрація роботи. Просто передайте те, про що насправді йдеться, "
    "у його власному типі змісту. Жодну тему не пропускайте, жодних двох "
    "тем не зливайте.\n\n"
    "У кожному абзаці природними реченнями передайте суть теми і ТІЛЬКИ "
    "ТІ конкретні деталі, що РЕАЛЬНО присутні в тексті: імена, числа, "
    "дати, назви, місця, терміни, інструменти, рішення, результати — "
    "дослівно так, як вони звучать. Вплітайте їх у речення, а не "
    "виносьте у списки чи поля.\n\n"
    "НАЙВАЖЛИВІШЕ ПРАВИЛО ПРОТИ ВИГАДОК: не існує «обов'язкових полів». "
    "Місце, дату, ім'я чи число згадуйте ЛИШЕ тоді, коли вони справді є в "
    "транскрипті. Якщо в тексті немає міста — НЕ додавайте жодного міста. "
    "Якщо немає дати — НЕ вигадуйте дати. Якщо немає чисел — не "
    "підставляйте. Порожнє поле краще за вигадане значення. Ви нічого не "
    "додаєте від себе — лише переказуєте те, що реально сказано.\n\n"
    "Кожен абзац — 2-5 повних речень суцільного тексту. Розділяйте "
    "абзаци порожнім рядком.\n\n"
    "ГОЛОВНИЙ ПРИНЦИП: ви переказуєте ЗМІСТ, а не відтворюєте мовлення. "
    "Не цитуйте і не переписуйте репліки, які не несуть змістової "
    "інформації (емоційні вигуки, звертання до глядача, слова-паразити). "
    "Залишайте лише те, що повідомляє конкретний зміст — факт, ідею, "
    "крок, ім'я, число, рішення, результат. Якщо речення не додає нічого "
    "нового до дайджесту, його не має бути у відповіді.\n\n"
    "Фрази без змістової цінності, які НЕ потрапляють у дайджест: "
    "вітання й прощання, звертання «з вами канал…», заклики підписатися "
    "чи поставити лайк, порожні зв'язки-тізери на кшталт «про це далі», "
    "«деталі згодом», вигуки «отже, поїхали».\n\n"
    "ВАЖЛИВО: якщо короткий анонс на початку САМ містить зміст (думку, "
    "число, факт), цей зміст має бути в дайджесті. Викидайте лише порожню "
    "обгортку тізера, а зміст із нього подайте разом із повним розкриттям "
    "тієї самої теми — одним абзацом, без повторення.\n\n"
    "НЕ повторюйте те саме у різних абзацах (анонс і основне розкриття — "
    "це одна тема). НЕ використовуйте markdown-заголовки (##, ###), "
    "розділювачі (---) чи жирний шрифт. НЕ вигадуйте нічого, чого немає в "
    "транскрипті.\n\n"
    "ЗАБОРОНЕНО виводити службові мітки на кшталт «ДЕ:», «ЩО:», «ХТО:», "
    "«СКІЛЬКИ:», «Тема:» — це інструкція для вас, а НЕ формат відповіді. "
    "Відповідь — лише суцільні речення.\n\n"
    "Відповідайте УКРАЇНСЬКОЮ мовою. Починайте відповідь ОДРАЗУ з першого "
    "абзацу про ПЕРШУ ЗМІСТОВНУ тему (не з привітання). Без вступних та "
    "підсумкових фраз."
)

_SYSTEM_PROMPT_EN = (
    "You are an assistant that produces a narrative digest of video "
    "transcripts of ANY kind (news, a lecture, a tutorial, a review, an "
    "interview, a vlog, a conversation). You write flowing paragraphs of "
    "prose — not lists, not tables, no section headings. You NEVER invent "
    "facts and add nothing that is not in the text: names, numbers, dates, "
    "locations, prices and terms are quoted verbatim, and if they are not "
    "in the transcript you do not mention them at all. Your summary's length "
    "tracks the content ACTUALLY present: a one-sentence fragment yields at "
    "most a one-sentence digest. You NEVER pad a thin fragment — you do not "
    "speculate about what the speaker 'likely' covers or 'goes on to "
    "discuss', do not continue the story beyond the text, and do not invent "
    "context, consequences or detail. If a fragment contains almost nothing, "
    "your output is almost nothing — that is correct, not a failure. You reply in the "
    "SAME language as the transcript, in ready-to-render markdown — no "
    "preamble, no commentary about yourself."
)

# Same rationale as _USER_PROMPT_UK: format rules go AFTER {transcript} so
# they survive Ollama's start-of-window truncation on long inputs.
_USER_PROMPT_EN = (
    "Below is a transcript of a video. Produce a narrative digest of it, "
    "following the instructions that come AFTER the transcript.\n\n"
    "TRANSCRIPT:\n{transcript}\n\n"
    "---\n"
    "Describe the content of the transcript above in flowing prose, "
    "paragraphs, accessible language. The video can be of ANY kind — news, "
    "a lecture, a tutorial, a review, an interview, a vlog, a "
    "conversation, a walkthrough. Just convey what is actually being said, "
    "in its own kind of content. Do not force it into a news shape.\n\n"
    "MUST SKIP (DO NOT INCLUDE IN THE DIGEST):\n"
    "Contentless fragments, not the substance of the video:\n"
    "— Greetings and sign-offs: \"Hello\", \"Welcome\", \"Stay with us\", "
    "\"Thanks for watching\".\n"
    "— Calls to subscribe and engage: \"Subscribe\", \"Like and comment\", "
    "\"Click the link in the description\".\n"
    "— Empty teasers (\"Coming up\", \"More on this later\") — skip the "
    "teaser itself, but cover its content in its natural place when the "
    "video unfolds it.\n"
    "— Donation pitches, ads, sponsor reads, filler exclamations.\n\n"
    "FORMAT:\n"
    "— One paragraph per distinct topic, idea, segment or story. As many "
    "paragraphs as the video has distinct topics. A short low-content "
    "video may fit in a single paragraph; a dense talk unfolds into "
    "several.\n"
    "— Each paragraph is 2-5 full sentences. Not chopped phrases, not "
    "bullet lists.\n"
    "— Inside the sentences, name ONLY the specifics that are ACTUALLY "
    "PRESENT in the text: names, numbers, dates, places, terms, tools, "
    "decisions, results — verbatim. Weave them into the prose, not into "
    "lists.\n"
    "— ANTI-FABRICATION RULE: there are no mandatory fields. Mention a "
    "place, a date, a name or a number ONLY when it genuinely appears in "
    "the transcript. If the text has no city, add no city. If it has no "
    "date, invent no date. An empty field is better than a fabricated "
    "value. Add nothing of your own — only retell what is actually said.\n"
    "— Do not repeat the same point across paragraphs (an intro teaser "
    "and the full coverage are one topic).\n"
    "— Do not use markdown headings (`##`, `###`), separators (`---`), "
    "or bold. Just clean paragraphs separated by a blank line.\n\n"
    "Reply in the SAME language as the transcript. No timecodes. No "
    "mentions of yourself as a model. If something isn't in the "
    "transcript, don't invent it. The FIRST sentence must be about the "
    "FIRST SUBSTANTIVE topic of the video — not a greeting, teaser or "
    "subscribe pitch. No closing summary at the end."
)


# --- Minimal prompts (experimental, env-gated) -----------------------------
#
# The heavy USER prompt above ("produce a narrative digest / FORMAT /
# paragraphs") implicitly mandates a paragraph-shaped output, which makes
# the 3B model PAD a sub-paragraph chunk with invented commentary (proven
# on the lone Kramer sentence: 9/9 prompt+temp combos fabricated). The
# minimal setup removes that pressure: a short system prompt + the raw
# chunk as the user message, with NO task framing at all. On the tiny chunk
# this yielded 1.0x size, 5/5 facts, zero fabrication, identical across
# temperatures. Gated behind DESCRIBELY_MINIMAL_PROMPT so it can be A/B'd
# on full transcripts before replacing the production templates.
_SYSTEM_PROMPT_EN_MINIMAL = (
    "You are an assistant that retells the meaningful content of a video "
    "transcript of ANY kind. Your task is to retell the given text in "
    "fewer words, without losing facts. You MAY describe the actions that "
    "happen in the text. A sentence with no facts and no actions IS SKIPPED. "
    "If a sentence is dense with facts, split it into several sentences — "
    "without inventing any new facts. "
    "You write flowing text of prose — not lists, "
    "not tables, no section headings. You NEVER invent facts and add nothing "
    "that is not in the text: names, numbers, dates, locations, prices and "
    "terms are quoted verbatim, and if they are not in the transcript you do "
    "not mention them at all. You reply in the SAME language as the "
    "transcript and NEVER output Chinese, Japanese, Korean or any other "
    "writing system that is not the transcript's own script. You reply as "
    "ready-to-use text. The answer is ONLY the finished "
    "text — no preamble from you, no commentary from you, no explanations "
    "from you."
)

_SYSTEM_PROMPT_UK_MINIMAL = (
    "Ви — асистент, що переказує змістовний транскрипт з відео "
    "будь-якого типу. Ваша задача — переказати поданий текст меншою "
    "кількістю слів, не втрачаючи фактів. Можна описувати дії, "
    "які відбуваються в тексті. Речення без фактів та дій ПРОПУСКАЄТЬСЯ. "
    "Якщо фактів дуже багато — розділяйте на "
    "кілька речень, не вигадуючи нових фактів. "
    "Ви викладаєте матеріал суцільним текстом прози — не "
    "списками, не таблицями, без рубрик. Ви НІКОЛИ не вигадуєте факти і не "
    "додаєте того, чого немає в тексті: імена, числа, дати, локації, ціни і "
    "терміни передаєте дослівно, а якщо їх у транскрипті немає — не "
    "згадуєте взагалі. Ви відповідаєте тією ж мовою, що й транскрипт, і "
    "НІКОЛИ не вживаєте китайських, японських, корейських чи будь-яких "
    "інших ієрогліфів — пишете ЛИШЕ українською абеткою. Відповідь у "
    "форматі готового тексту. Відповідь — це ТІЛЬКИ готовий текст: без "
    "преамбули від вас, без коментарів від вас, без пояснень від вас."
)


def _build_minimal_prompts(
    *, transcript_text: str, language: Optional[str]
) -> tuple[str, str]:
    """Minimal mode: short system prompt + the raw chunk as the user prompt.

    No task framing, no FORMAT block — so nothing pressures the model to
    expand a thin chunk into a paragraph. For non-UK/EN languages the
    English system prompt carries a "write in {Language}" directive.
    """
    code = (language or "").strip().lower()
    if code in ("uk", "ukrainian"):
        return _SYSTEM_PROMPT_UK_MINIMAL, transcript_text
    if code in ("en", "english"):
        return _SYSTEM_PROMPT_EN_MINIMAL, transcript_text
    name = _resolve_language_name(language) or (language or "the transcript's language")
    system = (
        _SYSTEM_PROMPT_EN_MINIMAL
        + f" The transcript is in {name}; write the digest in {name}."
    )
    return system, transcript_text


# --- Stage 3: per-chunk title -----------------------------------------------
#
# After a chunk is summarised, a short title is generated for that chunk's
# summary so the digest reads as titled sections (## title + body). The
# title conveys the gist/vibe in a few words; it must invent nothing and
# stay in the transcript's language/script (same no-hieroglyph guard as the
# summary prompts).

_TITLE_SYSTEM_UK = (
    "Ти добираєш дуже короткий заголовок (2-5 слів) українською, що передає "
    "тему й настрій поданого тексту. Заголовок ґрунтується ЛИШЕ на тексті — "
    "нічого не вигадуй. Відповідай ТІЛЬКИ заголовком: без лапок, без крапки "
    "в кінці, без пояснень, лише українською абеткою (ніяких ієрогліфів)."
)

_TITLE_SYSTEM_EN = (
    "You write a very short title (2-5 words) that conveys the topic and vibe "
    "of the given text. Base it ONLY on the text — invent nothing. Reply with "
    "ONLY the title: no quotes, no trailing period, no explanation, in the "
    "SAME language as the text, and never use Chinese, Japanese, Korean or any "
    "other foreign script."
)


def _build_title_prompts(
    *, text: str, language: Optional[str]
) -> tuple[str, str]:
    """Return (system, user) for the per-chunk title call; user == the text."""
    code = (language or "").strip().lower()
    if code in ("uk", "ukrainian"):
        return _TITLE_SYSTEM_UK, text
    if code in ("en", "english"):
        return _TITLE_SYSTEM_EN, text
    name = _resolve_language_name(language) or (language or "the transcript's language")
    return _TITLE_SYSTEM_EN + f" Write the title in {name}.", text


def _clean_title(raw: str) -> str:
    """Tidy a model-produced title into a single clean heading line."""
    line = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    # Strip leading markdown heading marks and surrounding quotes/trailing dot.
    line = line.lstrip("#").strip().strip('"').strip("'").strip()
    return line.rstrip(".").strip()


# --- Prev-context prompts (stage 1, chunks after the first) ----------------
#
# For every chunk except the first, the model is given the FULL previous
# chunk as context-only plus the current chunk, and asked to retell ONLY
# the current chunk. This resolves a story that straddles the boundary
# without re-summarising the previous chunk (which would duplicate it).
# Kept deliberately lean: the heavy anti-fabrication / skip-filler guards
# live in the shared system prompt (_SYSTEM_PROMPT_UK/EN), which is reused
# here; the user prompt only frames the two fragments. The format rules
# sit AFTER the data for the same start-of-window-truncation reason as the
# main template.
_USER_PREVCTX_UK = (
    "ПОПЕРЕДНІЙ КОНТЕКСТ (лише для розуміння, НЕ переказуйте його):\n"
    "{prev}\n\n"
    "НОВИЙ ФРАГМЕНТ (переказуйте САМЕ цей фрагмент):\n"
    "{cur}\n\n"
    "---\n"
    "Зробіть наративний дайджест НОВОГО ФРАГМЕНТА за фактами. Попередній "
    "контекст використовуйте лише щоб правильно зрозуміти, про що йдеться "
    "на початку нового фрагмента (продовження події, займенники, "
    "недомовлені назви) — але НЕ переказуйте події з контексту повторно. "
    "Передавайте факти, імена, числа, дати й локації дослівно — і ЛИШЕ "
    "ті, що справді є в тексті; нічого не вигадуйте. Без заголовків. "
    "Відповідайте УКРАЇНСЬКОЮ."
)

_USER_PREVCTX_EN = (
    "PREVIOUS CONTEXT (for understanding only, do NOT retell it):\n"
    "{prev}\n\n"
    "NEW FRAGMENT (retell THIS fragment):\n"
    "{cur}\n\n"
    "---\n"
    "Produce a narrative digest of the NEW FRAGMENT, by its facts. Use the "
    "previous context only to correctly understand what the start of the "
    "new fragment refers to (a continuing event, pronouns, half-said "
    "names) — but do NOT retell events from the context again. Carry over "
    "facts, names, numbers, dates and locations verbatim — and ONLY those "
    "that genuinely appear in the text; invent nothing. No headings. Reply "
    "in the SAME language as the transcript."
)


def _build_prevctx_prompts(
    *, prev_text: str, cur_text: str, language: Optional[str]
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for a prev-context chunk call.

    Mirrors :func:`_build_prompts`' language handling: native UK/EN
    templates, else the English template plus an explicit "transcript is
    in {Language}" directive appended to the trailing (never-truncated)
    block. The system prompt is the SAME shared guard as the first-chunk
    call, so the anti-fabrication rules apply identically.
    """
    code = (language or "").strip().lower()
    if code in ("uk", "ukrainian"):
        return _SYSTEM_PROMPT_UK, _USER_PREVCTX_UK.format(prev=prev_text, cur=cur_text)
    if code in ("en", "english"):
        return _SYSTEM_PROMPT_EN, _USER_PREVCTX_EN.format(prev=prev_text, cur=cur_text)

    name = _resolve_language_name(language) or (language or "the transcript's language")
    directive = (
        f"\n\nIMPORTANT: the transcript is in {name}. Write the entire "
        f"summary in {name}."
    )
    return _SYSTEM_PROMPT_EN, _USER_PREVCTX_EN.format(
        prev=prev_text, cur=cur_text
    ) + directive


def _build_prompts(
    *, transcript_text: str, language: Optional[str]
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) localized to ``language``.

    Picks the native Ukrainian or English template when the transcript is
    in one of those. For anything else, the English template is used and
    we append an explicit "Write the summary in {Language}" line so the
    model writes the summary in that language even though it received the
    instructions in English.
    """
    # CHOSEN APPROACH: minimal prompt — short system prompt + the raw chunk
    # as the user message, with no FORMAT block. The heavy templated prompt
    # below is DISABLED (kept commented for reference).
    return _build_minimal_prompts(
        transcript_text=transcript_text, language=language
    )

    # --- heavy templated prompt (DISABLED) ---------------------------------
    # code = (language or "").strip().lower()
    # if code in ("uk", "ukrainian"):
    #     return _SYSTEM_PROMPT_UK, _USER_PROMPT_UK.format(transcript=transcript_text)
    # if code in ("en", "english"):
    #     return _SYSTEM_PROMPT_EN, _USER_PROMPT_EN.format(transcript=transcript_text)
    #
    # # Other languages: English instructions + explicit language directive.
    # name = _resolve_language_name(language) or (language or "the transcript's language")
    # directive = (
    #     f"\n\nIMPORTANT: the transcript is in {name}. Write the entire "
    #     f"summary in {name}."
    # )
    # return _SYSTEM_PROMPT_EN, _USER_PROMPT_EN.format(
    #     transcript=transcript_text
    # ) + directive


# --- LLM caller ------------------------------------------------------------


def _utterances_to_plain_text(utterances) -> str:
    # Join on NEWLINE, not space. ASR utterances often lack end punctuation;
    # space-joining fuses them into run-on text with no sentence boundary,
    # which shifts where chunk_text splits and, on one test2 chunk, packed a
    # repetitive ASR run into a single chunk that tipped qwen2.5:3b into a
    # decode loop. One utterance per line mirrors the readable .transcription
    # .md the cleanup writer emits (and the text the tuning sweeps validated
    # on), so the production digest reproduces the swept prev-context result.
    return "\n".join(u.text.strip() for u in utterances if u.text.strip())


def _llm_failure_message(exc: Exception) -> str:
    """Turn an LLM exception into an actionable warning for the user."""
    detail = str(exc)
    low = detail.lower()
    head = "Summary skipped — LLM call failed."
    if "unexpectedly stopped" in low or "resource" in low or "memory" in low:
        hint = (
            " The model ran out of memory (common on 16 GB Macs after "
            "Whisper has run). Free up RAM, or install a lighter text model "
            "(e.g. `ollama pull qwen2.5:3b`) and set it as the summary model."
        )
    elif "unavailable" in low or "connect" in low:
        hint = " Ollama is not reachable — start it with `ollama serve`."
    elif "not found" in low:
        hint = " The configured model isn't installed — try `ollama pull qwen2.5:7b`."
    else:
        hint = ""
    return f"{head}{hint} (cause: {detail})"


# --- Public façade ---------------------------------------------------------


class Summarizer:
    """Single LLM call → ready-to-write markdown.

    Returns the markdown string on success. Returns ``None`` when the
    summary was intentionally skipped (mode == "none" or empty
    transcript). Raises :class:`SummarizationError` (or a subclass)
    when the LLM call itself fails — the caller decides whether to
    surface the error or quietly drop the artefact.
    """

    def __init__(
        self,
        options: SummaryOptions,
        llm_client: Optional[_LLMClientProtocol] = None,
        logger_fn=None,
    ) -> None:
        self.options = options
        self._llm = llm_client
        self._logger = logger_fn or (lambda message: None)

    def summarize(
        self,
        transcript: Transcript,
        language: Optional[str] = None,
        progress_callback: Optional[ProgressCallback] = None,
        partial_callback: Optional[Callable[[str], None]] = None,
        cancel_event=None,
    ) -> Optional[str]:
        # ``partial_callback`` receives each per-chunk summary the moment it
        # is produced, so the caller can stream it to disk (the summary file
        # is written after the first chunk and appended after each one). It is
        # invoked with the chunk's text in order; never with empty text.
        # ``cancel_event`` (a threading.Event) lets a long run stop between
        # chunks; on cancel we return whatever was produced so far rather than
        # raising, so the partial file the caller streamed is kept intact.
        def _partial(text: str) -> None:
            if partial_callback is None:
                return
            cleaned = (text or "").strip()
            if not cleaned:
                return
            try:
                partial_callback(cleaned)
            except Exception as exc:  # noqa: BLE001 - streaming must not break a run
                self._logger(f"[summarize] partial write failed: {exc}")

        def _cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        if self.options.mode == "none" or not transcript.utterances:
            _emit(progress_callback, 1.0)
            return None

        if self._llm is None:
            raise ProviderUnavailableError(
                "No LLM client is configured. Summary requires Ollama (or a "
                "compatible backend) to be reachable."
            )

        try:
            available = self._llm.is_available()
        except Exception as exc:  # noqa: BLE001 - defensive
            raise ProviderUnavailableError(
                f"LLM availability check raised: {exc}"
            ) from exc
        if not available:
            raise ProviderUnavailableError(
                "Configured LLM is not reachable right now."
            )

        full_text = _utterances_to_plain_text(transcript.utterances)
        if not full_text.strip():
            _emit(progress_callback, 1.0)
            return None

        _emit(progress_callback, 0.05)

        # One flat level of chunking: summarise each chunk, then stitch
        # the per-chunk summaries at their seams. No recursion, no
        # second-level re-summarisation.
        target = _env_int("DESCRIBELY_CHUNK_TOKENS", _CHUNK_TARGET_TOKENS)
        floor = _env_int("DESCRIBELY_MIN_CHUNK_TOKENS", _MIN_CHUNK_TOKENS)
        chunks = chunk_text(
            full_text,
            target_tokens=max(target, floor),
            overlap_tokens=_env_int("DESCRIBELY_CHUNK_OVERLAP", _CHUNK_OVERLAP_TOKENS),
        )
        if not chunks:
            _emit(progress_callback, 1.0)
            return None

        # Progress budget across the three stages:
        #   0.05..0.70  stage 1 — per-chunk summaries (the bulk of the work)
        #   0.70..0.85  stage 2 — fold-merge stitch of the per-chunk summaries
        #   0.85..1.00  stage 3 — publicistic formatting of the digest
        # A single-chunk transcript skips stages 1's loop and 2 (nothing to
        # stitch) and jumps straight to formatting.
        if len(chunks) == 1:
            summary = self._summarize_one(chunks[0], language=language)
            digest = self._titled_block(summary, language=language)
            _dump_chunks_if_requested(chunks, [digest], self._logger)
            _partial(digest)
            _emit(progress_callback, 0.85)
        else:
            # CHOSEN APPROACH: summarise each chunk STANDALONE (the model
            # sees ONLY its own chunk) and concatenate the per-chunk
            # summaries with a blank-line seam. Validated on EN news +
            # spontaneous UK speech: with the minimal prompt and qwen2.5:7b
            # this keeps facts, drops the editorial cruft, and never balloons
            # a thin chunk. The prev-context and fold-merge stitch techniques
            # are DISABLED (kept commented below for reference).
            partials: list[str] = []
            for i, chunk in enumerate(chunks):
                # Stop between chunks if cancelled — keep what we have so the
                # streamed partial file is preserved (no raise, no data loss).
                if _cancelled():
                    self._logger(
                        f"[summarize] cancelled after {i}/{len(chunks)} chunks"
                    )
                    break
                self._logger(
                    f"[summarize] chunk {i + 1}/{len(chunks)} ({len(chunk)} chars)"
                )
                summary = self._summarize_one(chunk, language=language)
                # Stage 3: give this chunk a short title, emit "## title + body".
                block = self._titled_block(summary, language=language)
                partials.append(block)
                # Stream this titled block to the caller immediately.
                _partial(block)
                # --- prev-context technique (DISABLED) -------------------
                # if i == 0:
                #     partials.append(self._summarize_one(chunk, language=language))
                # else:
                #     partials.append(self._summarize_prevctx(
                #         chunks[i - 1], chunk, language=language))
                _emit(progress_callback, 0.05 + 0.65 * (i + 1) / len(chunks))

            _dump_chunks_if_requested(chunks, partials, self._logger)
            self._logger(
                f"[summarize] concatenating {len(partials)} standalone chunk summaries"
            )
            digest = "\n\n".join(p.strip() for p in partials if p and p.strip())
            # --- fold-merge stitch technique (DISABLED) ------------------
            # digest = stitch_summaries(
            #     partials, self._llm, language=language, logger_fn=self._logger)
            _emit(progress_callback, 0.85)

        # On cancel, return the partial digest (possibly empty) without
        # raising: the caller already streamed these chunks to disk and marks
        # the job cancelled itself.
        if _cancelled():
            _emit(progress_callback, 1.0)
            return digest.strip() if digest else None

        if not digest or not digest.strip():
            raise SummarizationError("LLM returned an empty summary.")

        # Stage 3 is now the per-chunk TITLE step (done inline above: each
        # block is "## title + body"). The older publicistic formatter
        # (format_digest) remains DISABLED — kept commented below as an
        # alternative. We ship the titled digest directly.
        _emit(progress_callback, 1.0)
        return digest.strip()

        # --- Stage 3 (disabled) -------------------------------------------
        # self._logger("[summarize] formatting digest into publicistic blocks")
        # markdown = format_digest(
        #     digest, self._llm, language=language, logger_fn=self._logger,
        # )
        # _emit(progress_callback, 1.0)
        # if not markdown or not markdown.strip():
        #     # Formatting failed — fall back to the unformatted digest.
        #     return digest.strip()
        # return markdown.strip()

    def _chat_clean(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        language: Optional[str],
        label: str,
    ) -> str:
        """``chat`` with a foreign-script guard: retry on CJK/RU drift.

        qwen2.5 occasionally abandons the target language mid-generation
        (Chinese, or Russian on Ukrainian input) despite the prompt anchor.
        We detect that and retry with a nudged temperature to escape the
        drift basin; any CJK that survives the retries is stripped so no
        hieroglyphs ever reach the user.
        """
        out = self._llm.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
        )
        if not _is_foreign_output(out, language):
            return out
        for attempt in (1, 2):
            self._logger(
                f"[summarize] {label}: foreign script detected, retry {attempt}"
            )
            out = self._llm.chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=min(0.8, temperature + 0.2 * attempt),
            )
            if not _is_foreign_output(out, language):
                return out
        # Still drifting after retries — strip CJK as a last resort.
        if _contains_cjk(out):
            self._logger(f"[summarize] {label}: stripping residual CJK")
            out = _strip_cjk(out)
        return out

    def _summarize_one(self, text: str, *, language: Optional[str]) -> str:
        """One LLM summarisation call on ``text``; return its markdown."""
        system_prompt, user_prompt = _build_prompts(
            transcript_text=text, language=language
        )
        # Low temperature keeps facts stable; staying above 0 avoids the
        # greedy-decode paragraph-loop that broke earlier iterations.
        return self._chat_clean(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.2,
            language=language,
            label="chunk summary",
        )

    def _titlize(self, summary_text: str, *, language: Optional[str]) -> str:
        """Stage 3: generate a short title for a chunk's summary.

        Returns a cleaned single-line title, or "" if generation fails or
        yields nothing — the caller then emits the body without a heading
        rather than breaking the run.
        """
        system_prompt, user_prompt = _build_title_prompts(
            text=summary_text, language=language
        )
        try:
            raw = self._chat_clean(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.2,
                language=language,
                label="title",
            )
        except Exception as exc:  # noqa: BLE001 - a title is optional, never fatal
            self._logger(f"[summarize] title generation failed: {exc}")
            return ""
        return _clean_title(raw)

    def _titled_block(self, summary_text: str, *, language: Optional[str]) -> str:
        """Combine a chunk summary with its generated title into one block.

        Produces ``## <title>\\n\\n<summary>`` so the digest reads as titled
        sections. SHORT summaries (< ``_TITLE_MIN_CHARS``) get no title — a
        one-or-two-sentence body doesn't need a heading. If no title could be
        made, returns just the summary body.
        """
        body = (summary_text or "").strip()
        if not body:
            return ""
        if len(body) < _TITLE_MIN_CHARS:
            return body
        title = self._titlize(body, language=language)
        if not title:
            return body
        return f"## {title}\n\n{body}"

    def _summarize_prevctx(
        self, prev_text: str, cur_text: str, *, language: Optional[str]
    ) -> str:
        """Summarise ``cur_text`` with ``prev_text`` as context-only input.

        The previous chunk disambiguates a story that straddles the chunk
        boundary; the model is told to retell ONLY the current chunk, so
        the boundary story is summarised once (no duplicate for the stitch
        to collapse). Same low temperature as the first-chunk call.
        """
        system_prompt, user_prompt = _build_prevctx_prompts(
            prev_text=prev_text, cur_text=cur_text, language=language
        )
        return self._llm.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.2,
        )


__all__ = ["ProgressCallback", "Summarizer"]
