"""Sequential fold-merge of per-chunk summaries.

The summarizer produces one summary per transcript chunk. Adjacent
chunk-summaries can repeat a story (a chunk boundary may fall inside a
report) or read abruptly across the join. We merge them into one digest
with a **sequential fold that re-chunks after every step**:

    1. Give the model the FULL previous text (the running ``carry``) and
       the FULL next chunk-summary, and ask it to merge them into one
       normalized, readable text without losing facts. Overlapping
       sentences are collapsed; the result is roughly A + B minus the
       duplicated diff.
    2. Re-chunk that merged text at the target token budget. Everything
       except the last chunk is "frozen" into the final digest — it is
       far enough from the next join that it will not change again. The
       last chunk becomes the ``carry`` fed into the next merge.
    3. Repeat with carry + C, carry + D, … Append the final carry at the
       end.

Because the model always sees two FULL adjacent pieces (not a narrow seam
window), it has the whole local context to decide what is a duplicate and
what is a distinct event — the small-window stitcher could not, and on
many seams it invented joins and looped. Re-chunking keeps the carry
bounded (~one chunk) so the merge prompt never grows without limit no
matter how long the transcript is.

The merge instructions live entirely in the SYSTEM prompt; the user
prompt carries only the two fragments as data.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Protocol

from app.services.chunking import chunk_text
from app.services.tokenization import count_tokens

LoggerFn = Callable[[str], None]


class _LLMClientProtocol(Protocol):
    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.0,
        num_predict_floor: Optional[int] = None,
    ) -> str: ...


# Token budget for the re-chunking step between merges. Kept in step with
# the summarizer's per-chunk target: the carry is ~one chunk, so the merge
# prompt is ~two chunks of input — small enough for the model to hold both
# in full, large enough that a single story is rarely split across the
# freeze/carry boundary.
_RECHUNK_TARGET_TOKENS = 1000


_SYSTEM_STITCH_UK = (
    "Ви — редактор, що поєднує два сусідні фрагменти одного дайджесту "
    "(ФРАГМЕНТ A і ФРАГМЕНТ B) в один суцільний читабельний текст.\n"
    "Кінець A і початок B можуть описувати ОДНУ Й ТУ САМУ подію різними "
    "словами — фрагменти зроблені з тексту, що частково накладається. Де "
    "так — це ОДНА подія: зведіть обидва описи в одне формулювання, "
    "узявши найповніше і додавши унікальні деталі з іншого; не пишіть її "
    "двічі. Де A і B описують РІЗНІ події — залиште обидві, одна за "
    "одною, нічого не зливаючи.\n"
    "Решту тексту, що не повторюється, передавайте дослівно — не "
    "переписуйте і не скорочуйте речення з фактами. Зберігайте поділ на "
    "абзаци (порожній рядок між темами).\n"
    "Ви НІКОЛИ не додаєте, не вигадуєте і не змінюєте факти, імена, "
    "числа, дати чи локації. Ви нічого не викидаєте зі змісту обох "
    "фрагментів. Відповідь — лише поєднаний текст УКРАЇНСЬКОЮ, без "
    "пояснень і передмов."
)

_USER_STITCH_UK = (
    "ФРАГМЕНТ A:\n{left}\n\n"
    "ФРАГМЕНТ B:\n{right}\n"
)

_SYSTEM_STITCH_EN = (
    "You are an editor merging two adjacent fragments of one digest "
    "(FRAGMENT A and FRAGMENT B) into a single continuous, readable "
    "text.\n"
    "The end of A and the start of B may describe THE SAME event in "
    "different words — the fragments were made from partially overlapping "
    "text. Where they do, it is ONE event: combine both descriptions into "
    "a single wording, taking the fullest and adding any unique details "
    "from the other; do not write it twice. Where A and B describe "
    "DIFFERENT events, keep both, one after another, merging nothing.\n"
    "Pass through any non-duplicated text verbatim — do not rewrite or "
    "shorten sentences that carry facts. Preserve paragraph breaks (a "
    "blank line between topics).\n"
    "You NEVER add, invent or change any fact, name, number, date or "
    "location. You drop nothing from the content of either fragment. "
    "Reply with the merged text only, no explanation, no preamble."
)

_USER_STITCH_EN = (
    "FRAGMENT A:\n{left}\n\n"
    "FRAGMENT B:\n{right}\n"
)


def _stitch_prompts(left: str, right: str, language: Optional[str]) -> tuple[str, str]:
    code = (language or "").strip().lower()
    if code in ("uk", "ukrainian"):
        return _SYSTEM_STITCH_UK, _USER_STITCH_UK.format(left=left, right=right)
    # English instructions cover every other language; the fragment text
    # itself carries the language, so the model echoes it back.
    return _SYSTEM_STITCH_EN, _USER_STITCH_EN.format(left=left, right=right)


def stitch_summaries(
    summaries: List[str],
    llm: _LLMClientProtocol,
    *,
    language: Optional[str] = None,
    rechunk_target_tokens: int = _RECHUNK_TARGET_TOKENS,
    logger_fn: Optional[LoggerFn] = None,
) -> str:
    """Fold ``summaries`` into one digest by sequential merge + re-chunk.

    For each adjacent pair the model sees both pieces IN FULL and returns
    a single merged text (A + B minus the overlapping diff). The merged
    text is re-chunked at ``rechunk_target_tokens``; all but the last
    re-chunk are frozen into the final digest, the last becomes the carry
    fed into the next merge. The final carry is appended at the end.
    """
    log = logger_fn or (lambda _msg: None)
    cleaned = [s.strip() for s in summaries if s and s.strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]

    frozen: List[str] = []  # paragraphs that will not change again
    carry = cleaned[0]      # text still in play for the next merge

    for i in range(1, len(cleaned)):
        nxt = cleaned[i]
        log(
            f"[merge] {i}/{len(cleaned) - 1} "
            f"(carry={count_tokens(carry)} tok, next={count_tokens(nxt)} tok)"
        )
        system_prompt, user_prompt = _stitch_prompts(carry, nxt, language)
        # The merged output is ~A+B; lift the reply budget so a two-chunk
        # merge never truncates mid-text. Combined input tokens is a safe
        # floor for the output (it can only shrink as duplicates collapse).
        floor = count_tokens(carry) + count_tokens(nxt)
        merged = llm.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
            num_predict_floor=floor,
        ).strip()
        if not merged:
            # Defensive: if the model returns nothing, fall back to a plain
            # concatenation so we never silently drop a chunk.
            merged = f"{carry}\n\n{nxt}"

        # Re-chunk the merged text. Freeze everything but the tail; the
        # tail stays in play because the next chunk-summary may still
        # describe the same event at the upcoming join.
        pieces = chunk_text(merged, target_tokens=rechunk_target_tokens)
        if len(pieces) <= 1:
            # Whole merge still fits one chunk: keep it all as carry, freeze
            # nothing yet (degenerate "carry the last chunk" with one chunk).
            carry = merged
        else:
            frozen.extend(pieces[:-1])
            carry = pieces[-1]

    if carry.strip():
        frozen.append(carry.strip())
    return "\n\n".join(p for p in frozen if p.strip())


__all__ = ["stitch_summaries"]
