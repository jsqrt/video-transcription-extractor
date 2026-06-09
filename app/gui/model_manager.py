"""Locate the embedded Whisper model directory.

The packaged app ships ``large-v3`` inside the bundle so the user never
needs to download anything on first run. We look for it in three
places, in priority order:

1. ``sys._MEIPASS / models / large-v3`` — PyInstaller extraction root.
2. ``<exe_dir> / models / large-v3`` — portable layouts where the
   model sits next to the launcher binary, plus the macOS
   ``Contents/Resources/`` cousin.
3. ``<repo_root> / models / large-v3`` — developer-mode fallback when
   running ``python -m app.gui`` from a checkout.

The model directory follows the **flat** snapshot layout produced by
``scripts/fetch_model.py`` (every file from the HuggingFace repo
directly under the directory). ``faster_whisper.WhisperModel`` accepts
that path as ``model_size_or_path`` without consulting the HF cache.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


EMBEDDED_MODEL_NAME = "large-v3"
EMBEDDED_MODEL_SUBDIR = Path("models") / EMBEDDED_MODEL_NAME

# The summarization LLM lives at a stable on-disk name regardless of
# which Qwen size was pre-seeded — see scripts/fetch_llm.py.
EMBEDDED_LLM_FILENAME = "describely-summary.gguf"
EMBEDDED_LLM_SUBDIR = Path("models") / "llm"

# whisper.cpp / GGML variant of the Whisper model. Shipped on macOS so
# Apple Silicon gets Metal acceleration that CTranslate2 cannot
# provide. See scripts/fetch_whisper_ggml.py.
#
# We pin EXACTLY ONE file rather than a fallback list. Earlier the code
# auto-picked the first existing file from a priority chain, which made
# the Speed-mode speed depend on which model the user had laying around
# from earlier installs (medium-q5_0 vs turbo-q5_0 vs large-v3-q5_0).
# large-v3-turbo-q5_0 (~575 MB) has the full large-v3 encoder (so
# Ukrainian / multilingual vocabulary accuracy is preserved) but a
# 4-layer decoder, running at roughly medium-q5_0 speed on Apple
# Silicon. That's the right trade-off for Speed mode.
EMBEDDED_WHISPER_GGML_FILENAME = "ggml-large-v3-turbo-q5_0.bin"
EMBEDDED_WHISPER_GGML_SUBDIR = Path("models") / "whisper-ggml"

# HuggingFace tokenizer.json used to size summarizer chunks by token
# count. We ship the Qwen2.5 tokenizer (the default summary model); for
# other models it is an estimate, which is fine for chunk boundaries.
# See scripts/fetch_summary_tokenizer.py and app/services/tokenization.py.
EMBEDDED_TOKENIZER_FILENAME = "tokenizer.json"
EMBEDDED_TOKENIZER_SUBDIR = Path("models") / "tokenizer"


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []

    # 1. PyInstaller bootstrap dir.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))

    # 2. Directory of the launching executable (frozen apps) or main script.
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
        macos_parent = Path(sys.executable).resolve().parent
        resources = macos_parent.parent / "Resources"
        if resources.exists():
            roots.append(resources)

    # 3. Repository root (dev mode).
    repo_root = Path(__file__).resolve().parents[2]
    roots.append(repo_root)

    return roots


def find_embedded_model_path() -> Optional[Path]:
    """Return the absolute path of the embedded model directory.

    The returned path points at the directory that contains ``model.bin``
    (and friends) — suitable to pass directly to
    ``faster_whisper.WhisperModel`` as the model identifier.

    Returns ``None`` if no embedded copy is found.
    """
    for root in _candidate_roots():
        candidate = root / EMBEDDED_MODEL_SUBDIR
        if (candidate / "model.bin").is_file():
            return candidate
    return None


# Kept for callers that still want the parent ``models/`` directory
# (e.g. legacy ``download_root`` plumbing).
def find_embedded_model_dir() -> Optional[Path]:
    path = find_embedded_model_path()
    return path.parent if path else None


def embedded_model_name() -> str:
    return EMBEDDED_MODEL_NAME


def find_embedded_whisper_ggml_path() -> Optional[Path]:
    """Return the absolute path of the bundled GGML Whisper model.

    Only present in macOS builds (or dev checkouts that have run
    ``scripts/fetch_whisper_ggml.py``). Returns ``None`` if the file
    is missing — caller falls back to FasterWhisperProvider.

    The file is fixed to :data:`EMBEDDED_WHISPER_GGML_FILENAME`
    (large-v3-turbo-q5_0). ``DESCRIBELY_WHISPER_GGML`` overrides the
    selection: set it to an absolute .bin path, or to a bare filename
    (e.g. ``ggml-large-v3-q5_0.bin``) to pick a different bundled
    variant without touching the source.
    """
    override = (os.environ.get("DESCRIBELY_WHISPER_GGML") or "").strip()
    if override:
        as_path = Path(override).expanduser()
        if as_path.is_file():
            return as_path
        for root in _candidate_roots():
            candidate = root / EMBEDDED_WHISPER_GGML_SUBDIR / override
            if candidate.is_file():
                return candidate
        # Fall through to the default resolution if the override misses.

    for root in _candidate_roots():
        candidate = (
            root / EMBEDDED_WHISPER_GGML_SUBDIR / EMBEDDED_WHISPER_GGML_FILENAME
        )
        if candidate.is_file():
            return candidate
    return None


def find_embedded_llm_path() -> Optional[Path]:
    """Return the absolute path of the bundled summarization LLM, or None.

    The runtime uses this with ``LlamaCppClient`` as a fallback when
    Ollama is not running. Returns ``None`` if the file is missing
    (development checkouts that never ran ``scripts/fetch_llm.py``).
    """
    for root in _candidate_roots():
        candidate = root / EMBEDDED_LLM_SUBDIR / EMBEDDED_LLM_FILENAME
        if candidate.is_file():
            return candidate
    return None


def find_embedded_tokenizer_path() -> Optional[Path]:
    """Return the absolute path of the bundled summary tokenizer, or None.

    Used by app/services/tokenization.py to count tokens the way the LLM
    does when sizing summarizer chunks. Returns ``None`` if the file is
    missing (a checkout that never ran scripts/fetch_summary_tokenizer.py),
    in which case the caller falls back to a chars/token heuristic.
    """
    for root in _candidate_roots():
        candidate = root / EMBEDDED_TOKENIZER_SUBDIR / EMBEDDED_TOKENIZER_FILENAME
        if candidate.is_file():
            return candidate
    return None


def user_data_dir(app_name: str = "Describely") -> Path:
    """Per-user writable directory for logs and overrides.

    Not used by the model loader (the model is read-only inside the
    bundle) but handy for logs and to advertise where to find them in
    error messages.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / app_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / app_name
