"""Stage 3: publicistic formatting of the stitched digest (hybrid).

Stages 1-2 (chunk-summarise + fold-merge stitch, see
``app.services.summarizer`` and ``app.services.seam_stitch``) produce one
flat digest: clean paragraphs of prose separated by blank lines, every
fact preserved, no headings. That is faithful but flat — a wall of
paragraphs with no structure for a reader to navigate.

This stage adds the structure WITHOUT letting the model touch the prose.
Earlier we asked the model to "reformat" the whole digest; on qwen2.5:7b
that reliably rewrote and compressed it (a 9.6k-char digest came back at
4.7k, with ~18 of 26 spot-checked facts gone — whole crime/legal stories
dropped). The model cannot reread a paragraph to "group" it without
regenerating — and regenerating means paraphrasing and shortening.

So we go hybrid:

1. Number the digest paragraphs (P1, P2, …).
2. Ask the model for a PLAN ONLY (structured JSON): which paragraph
   indices group together, a publicistic heading for each group, and a
   few practical recommendations per group. The model never emits the
   paragraph text, so it physically cannot alter or drop a fact.
3. Reassemble the markdown in code: ``## heading`` + the VERBATIM
   paragraphs for that group's indices + an optional ``### Рекомендації``
   block. Any paragraph the plan forgot is appended verbatim under a
   trailing block, so nothing is ever lost even if the plan is partial.

Chunking is by paragraph: a long digest is planned in windows of up to
``_FORMAT_TARGET_TOKENS`` tokens so the planning prompt never floods the
context. Indices in each window are local to that window.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Protocol

from app.services.tokenization import count_tokens

LoggerFn = Callable[[str], None]


class _LLMClientProtocol(Protocol):
    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        response_schema: Optional[dict] = None,
    ) -> dict: ...


# Token budget per planning window. The digest is already ~10x compressed,
# so 3-4k tokens covers a large slice (often the whole thing) in one pass —
# enough for the model to see related paragraphs together and group them
# under shared headings, small enough not to flood the context. We pack
# WHOLE paragraphs up to this budget.
_FORMAT_TARGET_TOKENS = 3500


# JSON Schema constraining the plan. Ollama 0.4+ guarantees this shape, so
# the parsing below never has to guess. ``paragraphs`` are 1-based indices
# into the window's paragraph list as shown to the model.
_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "paragraphs": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "recommendations": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["heading", "paragraphs"],
            },
        }
    },
    "required": ["blocks"],
}


_SYSTEM_PLAN_UK = (
    "Ви — редактор, що складає ПЛАН верстки готового тексту. Ви НЕ "
    "переписуєте текст — ви лише вирішуєте, як його згрупувати й "
    "озаголовити.\n"
    "Вам дають пронумеровані абзаци (1, 2, 3, …). Згрупуйте СУСІДНІ абзаци "
    "за спільною темою у блоки. Для кожного блоку поверніть:\n"
    "— heading: короткий змістовний заголовок у живому публіцистичному "
    "тоні УКРАЇНСЬКОЮ;\n"
    "— paragraphs: список номерів абзаців цього блоку (саме номери, не "
    "текст);\n"
    "— recommendations: 1–3 короткі практичні поради УКРАЇНСЬКОЮ, що "
    "випливають із фактів блоку (порожній список, якщо порад немає сенсу "
    "давати).\n"
    "ОБОВʼЯЗКОВО: кожен абзац має потрапити рівно в ОДИН блок; не "
    "пропускайте номери і не повторюйте їх. Зберігайте порядок (номери в "
    "межах блоку — зростаючі, блоки — за порядком абзаців). НЕ повертайте "
    "текст абзаців — лише їхні номери."
)

_SYSTEM_PLAN_EN = (
    "You are an editor producing a LAYOUT PLAN for a finished text. You do "
    "NOT rewrite the text — you only decide how to group and title it.\n"
    "You are given numbered paragraphs (1, 2, 3, …). Group ADJACENT "
    "paragraphs by shared theme into blocks. For each block return:\n"
    "— heading: a short, meaningful heading in a lively publicistic tone, "
    "in the SAME language as the paragraphs;\n"
    "— paragraphs: the list of paragraph numbers in this block (numbers, "
    "not text);\n"
    "— recommendations: 1-3 short practical takeaways, in the same "
    "language, that follow from the block's facts (empty list if none make "
    "sense).\n"
    "MUST: every paragraph goes in exactly ONE block; do not skip or repeat "
    "numbers. Preserve order (numbers within a block ascending, blocks in "
    "paragraph order). Do NOT return the paragraph text — only their "
    "numbers."
)

_RECS_HEADING_UK = "### Рекомендації"
_RECS_HEADING_EN = "### Recommendations"


def _is_uk(language: Optional[str]) -> bool:
    return (language or "").strip().lower() in ("uk", "ukrainian")


def _plan_prompts(paragraphs: List[str], language: Optional[str]) -> tuple[str, str]:
    system = _SYSTEM_PLAN_UK if _is_uk(language) else _SYSTEM_PLAN_EN
    numbered = "\n\n".join(f"[{i + 1}] {p}" for i, p in enumerate(paragraphs))
    user = (
        ("ПРОНУМЕРОВАНІ АБЗАЦИ:\n" if _is_uk(language) else "NUMBERED PARAGRAPHS:\n")
        + numbered
        + "\n"
    )
    return system, user


def _split_paragraphs(text: str) -> List[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _pack_windows(paragraphs: List[str], target_tokens: int) -> List[List[str]]:
    """Group paragraphs into windows of ≤target_tokens (whole paragraphs)."""
    windows: List[List[str]] = []
    buffer: List[str] = []
    buffer_tokens = 0
    for para in paragraphs:
        t = count_tokens(para)
        if buffer and buffer_tokens + t > target_tokens:
            windows.append(buffer)
            buffer, buffer_tokens = [], 0
        buffer.append(para)
        buffer_tokens += t
    if buffer:
        windows.append(buffer)
    return windows


def _render_window(
    paragraphs: List[str], plan: dict, language: Optional[str]
) -> str:
    """Assemble markdown from VERBATIM paragraphs + the model's plan.

    The plan only carries headings, paragraph indices and recommendations;
    the paragraph prose is taken untouched from ``paragraphs``. Any index
    the plan omitted (or that is out of range) is collected and appended
    verbatim at the end so no paragraph is ever dropped.
    """
    recs_heading = _RECS_HEADING_UK if _is_uk(language) else _RECS_HEADING_EN
    blocks = plan.get("blocks") if isinstance(plan, dict) else None
    if not isinstance(blocks, list):
        blocks = []

    out: List[str] = []
    used: set[int] = set()
    for block in blocks:
        if not isinstance(block, dict):
            continue
        heading = str(block.get("heading", "")).strip()
        idxs = block.get("paragraphs")
        if not isinstance(idxs, list):
            continue
        # 1-based → 0-based; keep only valid, not-yet-used, in given order.
        body: List[str] = []
        for raw in idxs:
            try:
                i = int(raw) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(paragraphs) and i not in used:
                used.add(i)
                body.append(paragraphs[i])
        if not body:
            continue
        if heading:
            out.append(f"## {heading}")
        out.append("\n\n".join(body))
        recs = block.get("recommendations")
        if isinstance(recs, list):
            bullets = [f"- {str(r).strip()}" for r in recs if str(r).strip()]
            if bullets:
                out.append(recs_heading + "\n" + "\n".join(bullets))

    # Safety net: any paragraph the plan forgot is appended verbatim so a
    # partial/garbled plan never loses content.
    missing = [paragraphs[i] for i in range(len(paragraphs)) if i not in used]
    if missing:
        out.append("\n\n".join(missing))

    return "\n\n".join(part for part in out if part.strip())


def format_digest(
    digest: str,
    llm: _LLMClientProtocol,
    *,
    language: Optional[str] = None,
    target_tokens: int = _FORMAT_TARGET_TOKENS,
    logger_fn: Optional[LoggerFn] = None,
) -> str:
    """Format ``digest`` into headed blocks + recommendations, losslessly.

    The model is asked for a PLAN ONLY (headings, paragraph indices,
    recommendations) via constrained JSON; the paragraph prose is taken
    verbatim from the digest in code. The digest is planned in windows of
    up to ``target_tokens`` tokens (by paragraph). If a planning call fails
    or returns nothing usable, that window falls back to its unformatted
    paragraphs so the content always survives.
    """
    log = logger_fn or (lambda _msg: None)
    body = (digest or "").strip()
    if not body:
        return ""

    paragraphs = _split_paragraphs(body)
    if not paragraphs:
        return ""

    windows = _pack_windows(paragraphs, target_tokens)
    rendered: List[str] = []
    for i, window in enumerate(windows):
        log(f"[format] window {i + 1}/{len(windows)} ({len(window)} paras)")
        system_prompt, user_prompt = _plan_prompts(window, language)
        try:
            plan = llm.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.0,
                response_schema=_PLAN_SCHEMA,
            )
        except Exception as exc:  # noqa: BLE001 - never lose content on a bad plan
            log(f"[format] window {i + 1} plan failed ({exc}); passing through")
            plan = {}
        rendered.append(_render_window(window, plan, language))

    return "\n\n".join(part for part in rendered if part.strip())


__all__ = ["format_digest"]
