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
