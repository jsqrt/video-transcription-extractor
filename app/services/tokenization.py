"""Token counting for summarizer chunk sizing.

The summarizer splits a long transcript into chunks of a fixed token
budget (see :mod:`app.services.summarizer`). To split the same way the
LLM reads text we count tokens with the bundled Qwen2.5 ``tokenizer.json``
(scripts/fetch_summary_tokenizer.py). Qwen2.5 is the default summary
model; for other installed models this is an estimate, which is all a
chunk boundary needs — a stable, repeatable split, not a bit-exact count.

If the tokenizer file or the ``tokenizers`` package is unavailable we
fall back to a character-per-token heuristic. Cyrillic encodes at
roughly 2 characters per token under the Qwen BPE (measured ~1.9 on
Ukrainian news text), so the estimate is close enough for chunk sizing.
"""

from __future__ import annotations

from typing import Optional

# Cyrillic BPE ratio used by the heuristic fallback. Matches the constant
# the Ollama provider uses for its own context-budget estimate.
_CHARS_PER_TOKEN = 2.0

# Lazily-loaded singleton. ``False`` means "tried and failed, use the
# fallback"; ``None`` means "not tried yet"; otherwise a live Tokenizer.
_tokenizer: object = None
_tokenizer_loaded = False


def _load_tokenizer() -> object:
    """Load the bundled tokenizer once; cache the result (or the failure).

    Returns the ``Tokenizer`` instance, or ``None`` if it could not be
    loaded — in which case callers use the character heuristic.
    """
    global _tokenizer, _tokenizer_loaded
    if _tokenizer_loaded:
        return _tokenizer
    _tokenizer_loaded = True
    try:
        from tokenizers import Tokenizer

        from app.gui.model_manager import find_embedded_tokenizer_path

        path = find_embedded_tokenizer_path()
        if path is None:
            _tokenizer = None
        else:
            _tokenizer = Tokenizer.from_file(str(path))
    except Exception:
        # Missing package, missing file, or a corrupt tokenizer.json — any
        # of these is non-fatal; chunk sizing degrades to the heuristic.
        _tokenizer = None
    return _tokenizer


def count_tokens(text: str) -> int:
    """Return the token count of ``text``.

    Uses the bundled Qwen2.5 tokenizer when available, otherwise a
    chars/token heuristic. Never raises — chunk sizing must not be able
    to crash summarization.
    """
    if not text:
        return 0
    tok = _load_tokenizer()
    if tok is not None:
        try:
            return len(tok.encode(text).ids)
        except Exception:
            pass
    return int(len(text) / _CHARS_PER_TOKEN)


def reset_tokenizer_cache() -> None:
    """Clear the cached tokenizer. For tests that toggle availability."""
    global _tokenizer, _tokenizer_loaded
    _tokenizer = None
    _tokenizer_loaded = False


__all__ = ["count_tokens", "reset_tokenizer_cache"]
