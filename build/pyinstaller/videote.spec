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

# ---- Stamp the build version ----------------------------------------------
# Write app/_build_info.py so the packaged app shows a concrete build version
# in the GUI (git isn't available inside a bundled app). Prefer an explicit
# VTE_BUILD_VERSION; otherwise derive it from git short hash + commit date.
def _stamp_build_version() -> None:
    import subprocess

    version = (os.environ.get("VTE_BUILD_VERSION") or "").strip()
    if not version:
        try:
            sha = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
            date = subprocess.check_output(
                ["git", "show", "-s", "--format=%cd", "--date=format:%Y-%m-%d",
                 "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True,
            ).strip()
            version = f"{date}+{sha}"
        except Exception:
            version = "unknown"
    (ROOT / "app" / "_build_info.py").write_text(
        f'BUILD_VERSION = "{version}"\n', encoding="utf-8"
    )
    print(f"[spec] stamped build version: {version}")


_stamp_build_version()

if not MODEL_DIR.is_dir():
    raise SystemExit(
        f"Embedded model not found at {MODEL_DIR}. "
        "Pre-seed it before building (see BUILD.md)."
    )

APP_NAME = "Describely"
APP_VERSION = "1.0.0"
BUNDLE_ID = "com.describely.app"
ENTRY = str(ROOT / "app" / "gui" / "__main__.py")

# macOS target architecture. ``universal2`` ships one bundle that runs
# natively on both Intel and Apple Silicon (PyInstaller merges per-arch
# native libs via ``lipo``). Override with ``VTE_MAC_ARCH=arm64`` or
# ``=x86_64`` to build a single-arch bundle (smaller, useful for CI).
# Ignored on Windows / Linux.
MAC_TARGET_ARCH = os.environ.get("VTE_MAC_ARCH", "universal2").strip() or None
if MAC_TARGET_ARCH not in (None, "universal2", "arm64", "x86_64"):
    raise SystemExit(
        f"Invalid VTE_MAC_ARCH={MAC_TARGET_ARCH!r}; expected one of "
        "universal2, arm64, x86_64."
    )

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

# Embedded summarization LLM (GGUF). Loaded by llama-cpp-python at
# runtime; see app/providers/llama_cpp_provider.py. The GGUF is a
# single ~2 GB file under models/llm/. Skipped silently if the
# maintainer didn't run scripts/fetch_llm.py — the runtime then falls
# back to Ollama / extractive.
LLM_DIR = ROOT / "models" / "llm"
if LLM_DIR.is_dir():
    datas.append((str(LLM_DIR), "models/llm"))

# Summary tokenizer (Qwen2.5 tokenizer.json, ~7 MB). Used by
# app/services/tokenization.py to size summarizer chunks by token count.
# All platforms. Skipped silently if scripts/fetch_summary_tokenizer.py
# wasn't run — the runtime then falls back to a chars/token estimate.
# Keep the filename in sync with model_manager.EMBEDDED_TOKENIZER_FILENAME.
TOKENIZER_FILE = ROOT / "models" / "tokenizer" / "tokenizer.json"
if TOKENIZER_FILE.is_file():
    datas.append((str(TOKENIZER_FILE), "models/tokenizer"))

# GGML Whisper model — needed by the whisper.cpp ASR path.
#   * Always shipped on macOS (default ASR backend).
#   * Shipped on Windows when the maintainer ran the build with
#     VTE_WHISPER_VULKAN=1 (i.e. a Vulkan-enabled pywhispercpp wheel is
#     present). Detected by importing the package and asking it for the
#     compiled-in backend list — cheaper and more reliable than parsing
#     an env var at build time.
WHISPER_GGML_DIR = ROOT / "models" / "whisper-ggml"
_ship_ggml = sys.platform == "darwin"
if not _ship_ggml:
    try:
        import pywhispercpp  # noqa: F401
        _ship_ggml = True
    except Exception:
        _ship_ggml = False
if _ship_ggml and WHISPER_GGML_DIR.is_dir():
    # Ship exactly the GGML weight file the runtime expects — keep this
    # filename in sync with app/gui/model_manager.py's
    # EMBEDDED_WHISPER_GGML_FILENAME. large-v3-turbo-q5_0 is the Speed
    # mode target: full large-v3 encoder (Ukrainian morphology stays
    # accurate) with a 4-layer decoder, ~575 MB, runs at roughly
    # medium-q5_0 speed on Apple Silicon.
    _ggml_file = WHISPER_GGML_DIR / "ggml-large-v3-turbo-q5_0.bin"
    if _ggml_file.is_file():
        datas.append((str(_ggml_file), "models/whisper-ggml"))

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
    # CUDA runtime — present on Windows/Linux x86_64 only. collect_all()
    # tolerates a missing import (see the try/except below), so macOS /
    # ARM builds will simply skip these and produce a CPU-only bundle.
    "nvidia",
    "nvidia.cublas",
    "nvidia.cudnn",
    # Embedded LLM backend. Brings in the llama.cpp shared library that
    # lives inside the wheel + its tokenizer / grammar files.
    "llama_cpp",
    # Whisper backend used on macOS (Metal). The package ships its own
    # native ``libwhisper.dylib`` next to the Python module — collect_all
    # pulls them in. On Windows / Linux pip skips installing this so the
    # try/except below tolerates the missing import.
    "pywhispercpp",
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
    "app.gui.update_prompt",
    "app.services",
    "app.services.pipeline",
    "app.providers.faster_whisper_provider",
    "app.providers.whisper_cpp_provider",
    "app.providers.ollama_provider",
    "app.providers.llama_cpp_provider",
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
    target_arch=MAC_TARGET_ARCH if is_macos else None,
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
