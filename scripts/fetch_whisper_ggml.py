"""Pre-seed the GGML Whisper model used by the macOS shipping bundle.

The Apple Silicon build uses ``whisper.cpp`` (via ``pywhispercpp``)
because CTranslate2 — the engine behind ``faster-whisper`` — has no
Metal backend. ``whisper.cpp`` does, and its model format is GGML,
distinct from the directory layout faster-whisper expects.

Run once on every macOS build host before the first PyInstaller run:

    python scripts/fetch_whisper_ggml.py

Output: ``models/whisper-ggml/ggml-large-v3.bin`` (~3.1 GB).

The downloaded file lives in a parallel directory to ``models/large-v3/``
so a single dev checkout can host both formats — handy when switching
between the macOS ``WhisperCppProvider`` and the Windows
``FasterWhisperProvider`` paths during development.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ggerganov's canonical whisper.cpp model repo on HuggingFace.
_REPO_ID = "ggerganov/whisper.cpp"
_FILENAME = "ggml-large-v3.bin"
_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "models" / "whisper-ggml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=str(_DEFAULT_DIR),
        help="Directory the GGML file will land in (default: ./models/whisper-ggml).",
    )
    parser.add_argument(
        "--variant",
        default="large-v3",
        choices=("large-v3", "large-v3-q5_0", "medium", "small"),
        help=(
            "Model variant (default: large-v3). The ``-q5_0`` quantized "
            "variant is ~1 GB instead of ~3 GB at a small accuracy cost."
        ),
    )
    args = parser.parse_args()

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print(
            "huggingface_hub is missing. Install runtime deps first:\n"
            "    pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    filename = f"ggml-{args.variant}.bin"
    target_dir = Path(args.root).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename

    print(f"Downloading {_REPO_ID} :: {filename}")
    print(f"           → {target_path}")
    print("(single ~3 GB file; expect a few minutes on a residential link)")

    downloaded = hf_hub_download(
        repo_id=_REPO_ID,
        filename=filename,
        local_dir=str(target_dir),
    )
    downloaded_path = Path(downloaded).resolve()

    if downloaded_path != target_path:
        if target_path.exists():
            target_path.unlink()
        downloaded_path.rename(target_path)

    if not target_path.is_file():
        print(f"Download finished but target is missing: {target_path}", file=sys.stderr)
        return 1

    size_mb = target_path.stat().st_size // (1024 * 1024)
    print(f"OK — {target_path} populated ({size_mb} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
