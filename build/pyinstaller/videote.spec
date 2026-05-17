# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Describely.

Builds a single --onedir application that bundles:

* The Python interpreter + all runtime deps.
* The Whisper ``large-v3`` model (~3 GB) at ``models/large-v3/``.
* The application icon (``icon.svg``).
* A platform-appropriate launcher (``Describely.exe`` on Windows,
  ``Describely.app`` on macOS).

The model is expected to live at ``<repo_root>/models/large-v3/`` before
building. Use ``scripts/fetch_model.py`` (or the manual snippet in
``BUILD.md``) to populate it.

Build:
    pyinstaller build/pyinstaller/videote.spec --noconfirm
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

# ---- Paths ----------------------------------------------------------------

ROOT = Path(os.environ.get("VTE_PROJECT_ROOT") or os.getcwd()).resolve()
MODEL_DIR = ROOT / "models" / "large-v3"
ICON_DIR = ROOT / "build" / "assets"
ICON_WIN = ICON_DIR / "app.ico"
ICON_MAC = ICON_DIR / "app.icns"

if not MODEL_DIR.is_dir():
    raise SystemExit(
        f"Embedded model not found at {MODEL_DIR}. "
        "Pre-seed it before building (see BUILD.md)."
    )

APP_NAME = "Describely"
APP_VERSION = "1.0.0"
BUNDLE_ID = "com.describely.app"
ENTRY = str(ROOT / "app" / "gui" / "__main__.py")

# ---- Data files -----------------------------------------------------------

# Ship the entire large-v3 directory next to the binary under models/.
datas = [(str(MODEL_DIR), "models/large-v3")]

# Application icon (SVG), loaded at runtime via QIcon.
SVG_ICON = ROOT / "build" / "assets" / "icon.svg"
if SVG_ICON.exists():
    datas.append((str(SVG_ICON), "."))

# Terms of Use, shipped next to the binary for in-app reference.
TERMS = ROOT / "TERMS.md"
if TERMS.exists():
    datas.append((str(TERMS), "."))

# macOS Quick Action template — Describely.app uses it to materialize
# Finder right-click menu entries on first launch (see
# app/gui/macos_integration.py). Bundle the whole template directory.
WORKFLOW_TEMPLATE = ROOT / "build" / "macos" / "_workflow_template"
if sys.platform == "darwin" and WORKFLOW_TEMPLATE.is_dir():
    datas.append((str(WORKFLOW_TEMPLATE), "_workflow_template"))

# imageio_ffmpeg packages an ffmpeg binary inside the wheel — pull it
# in as data so the GUI bundle does not depend on a system ffmpeg.
datas += collect_data_files("imageio_ffmpeg")
binaries = collect_dynamic_libs("av")

# Heavy ML packages: each one has lazy / private submodules and native
# binaries that PyInstaller's static analyzer routinely misses. The
# safest fix is collect_all(), which returns (binaries, datas, hidden)
# for every importable name inside the package.
#
# numpy 2.x in particular ships private submodules like
# ``numpy._core._exceptions`` that are imported lazily on first use;
# without this loop the bundle starts and then crashes on the first
# ``import numpy`` indirectly performed by faster_whisper.
_HEAVY_PACKAGES = (
    "numpy",
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "huggingface_hub",
    "onnxruntime",
    "av",
)
_collected_hidden: list[str] = []
for _pkg in _HEAVY_PACKAGES:
    try:
        _bins, _datas, _hidden = collect_all(_pkg)
    except Exception:
        # Package not installed — skip silently. onnxruntime is optional.
        continue
    binaries += _bins
    datas += _datas
    _collected_hidden += _hidden

# ---- Hidden imports -------------------------------------------------------

hiddenimports = _collected_hidden + [
    "httpx",
    "imageio_ffmpeg",
    "app",
    "app.gui",
    "app.gui.main_window",
    "app.gui.worker",
    "app.gui.model_manager",
    "app.gui.first_run",
    "app.gui.app_logger",
    "app.gui.macos_integration",
    "app.services",
    "app.services.pipeline",
    "app.providers.faster_whisper_provider",
    "app.providers.ollama_provider",
    "app.security.network_isolation",
]

# ---- Analysis -------------------------------------------------------------

a = Analysis(
    [ENTRY],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Trim unused stdlib + dev-only deps to keep the bundle smaller.
        "tkinter",
        "test",
        "unittest",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

# ---- Build target ---------------------------------------------------------

is_macos = sys.platform == "darwin"
is_windows = sys.platform == "win32"

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ICON_WIN) if (is_windows and ICON_WIN.exists()) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

if is_macos:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=str(ICON_MAC) if ICON_MAC.exists() else None,
        bundle_identifier=BUNDLE_ID,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "CFBundleIdentifier": BUNDLE_ID,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "NSHumanReadableCopyright": "Copyright (c) 2026 Describely contributors. MIT License.",
            # Receive file open events from Finder/LaunchServices so the
            # macOS Quick Action can launch the .app with the selected
            # files instead of a separate shell wrapper.
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "Media",
                    "CFBundleTypeRole": "Viewer",
                    "LSItemContentTypes": [
                        "public.movie",
                        "public.audio",
                        "public.audiovisual-content",
                    ],
                }
            ],
        },
    )
