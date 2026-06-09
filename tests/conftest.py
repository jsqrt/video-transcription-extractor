"""Pytest bootstrap.

Adds the project root to ``sys.path`` so ``import app`` works, and — when
the repository ships a Windows-style ``.venv`` — also opportunistically
exposes its ``site-packages`` so pure-Python deps like ``httpx`` are
available in sandboxed CI environments that cannot reach PyPI.

Nothing here is required at production runtime.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent


def _prepend(path: Path) -> None:
    value = str(path)
    if path.exists() and value not in sys.path:
        sys.path.insert(0, value)


_prepend(_PROJECT_ROOT)

# Developer convenience: pick up pure-Python libs shipped inside a local
# Windows venv when running tests on Linux sandboxes.
for relative in (
    Path(".venv") / "Lib" / "site-packages",
    Path(".venv") / "lib" / "site-packages",
):
    _prepend(_PROJECT_ROOT / relative)

# Ensure tests do not accidentally patch the global socket module when run
# in sequence. Each test that needs isolation must call `_reset_for_tests`.
os.environ.setdefault("VTE_TESTING", "1")
