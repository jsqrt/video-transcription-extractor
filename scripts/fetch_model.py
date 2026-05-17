"""Pre-seed the Whisper ``large-v3`` model into ``models/large-v3/``.

Run once before the first PyInstaller build. Pulls from HuggingFace
without going through ``app.security.network_isolation`` (this script
is not imported by the runtime app, so the socket guard never fires
here).

The output layout is intentionally **flat** — every file from the
HuggingFace repo lands directly under ``models/large-v3/``:

    models/large-v3/
        config.json
        model.bin
        preprocessor_config.json
        tokenizer.json
        vocabulary.json
        vocabulary.txt

This matches what ``faster_whisper.WhisperModel`` accepts when given
the directory path as ``model_size_or_path`` — no HuggingFace cache
resolution needed at runtime. It also makes PyInstaller bundling
straightforward (one directory, no `models--<org>--<repo>/snapshots/...`
nesting).

Usage:
    python scripts/fetch_model.py
    python scripts/fetch_model.py --model small  # smaller dev build
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# faster-whisper maps short model names to upstream HuggingFace repos.
# Keep this list in sync with faster-whisper's own mapping; you only
# need entries for the models you actually ship.
_MODEL_TO_REPO = {
    "tiny":      "Systran/faster-whisper-tiny",
    "tiny.en":   "Systran/faster-whisper-tiny.en",
    "base":      "Systran/faster-whisper-base",
    "base.en":   "Systran/faster-whisper-base.en",
    "small":     "Systran/faster-whisper-small",
    "small.en":  "Systran/faster-whisper-small.en",
    "medium":    "Systran/faster-whisper-medium",
    "medium.en": "Systran/faster-whisper-medium.en",
    "large-v1":  "Systran/faster-whisper-large-v1",
    "large-v2":  "Systran/faster-whisper-large-v2",
    "large-v3":  "Systran/faster-whisper-large-v3",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="large-v3", choices=sorted(_MODEL_TO_REPO))
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent / "models"),
        help="Local directory that will hold <model>/ (default: ./models)",
    )
    args = parser.parse_args()

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(
            "huggingface_hub is missing. Install runtime deps first:\n"
            "    pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    repo_id = _MODEL_TO_REPO[args.model]
    target = Path(args.root).resolve() / args.model
    target.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {repo_id}")
    print(f"           → {target}")
    print("(may take a few minutes; ~3 GB for large-v3)")

    # Newer huggingface_hub (≥0.23) always uses copy mode for local_dir;
    # the old ``local_dir_use_symlinks`` flag is deprecated and ignored.
    snapshot_download(repo_id=repo_id, local_dir=str(target))

    # Sanity check: the directory must hold the model weight file.
    if not (target / "model.bin").is_file():
        print(
            f"Download completed but model.bin is missing under {target}. "
            "Network glitch? Re-run the script.",
            file=sys.stderr,
        )
        return 1

    size_mb = sum(p.stat().st_size for p in target.rglob("*") if p.is_file()) // (1024 * 1024)
    print(f"OK — {target} populated ({size_mb} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
