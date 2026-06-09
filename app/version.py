"""Build version shown in the GUI.

``BUILD_VERSION`` is the single source of truth for the running build's
identity. It is overwritten at package time (the PyInstaller spec stamps the
release string here). In a dev checkout it stays ``"dev"`` and
:func:`build_version` falls back to the current git short hash + commit date,
so whatever is running is always identifiable in the UI.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

# Stamped at build time: the PyInstaller spec writes app/_build_info.py with
# the release string (see build/pyinstaller/videote.spec). In a dev checkout
# that generated file is absent, so BUILD_VERSION stays "dev" and
# build_version() falls back to the live git short hash + commit date.
try:
    from app._build_info import BUILD_VERSION  # type: ignore
except Exception:  # noqa: BLE001
    BUILD_VERSION = "dev"


@lru_cache(maxsize=1)
def build_version() -> str:
    """Return a human-readable build identifier for display in the GUI."""
    if BUILD_VERSION and BUILD_VERSION != "dev":
        return BUILD_VERSION
    # Dev checkout — derive something useful from git so the build shown in
    # the window always matches the code that's running.
    try:
        root = Path(__file__).resolve().parent.parent
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root, stderr=subprocess.DEVNULL, text=True,
        ).strip()
        date = subprocess.check_output(
            ["git", "show", "-s", "--format=%cd", "--date=format:%Y-%m-%d", "HEAD"],
            cwd=root, stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return f"dev-{sha} ({date})"
    except Exception:  # noqa: BLE001 - version display must never crash startup
        return "dev"


__all__ = ["BUILD_VERSION", "build_version"]
