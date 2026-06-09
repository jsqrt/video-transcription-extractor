"""Pre-seed the embedded summarization LLM into ``models/llm/``.

Describely ships a small Qwen 2.5 Instruct model in GGUF format so it
can produce real abstractive summaries even when the user has no
Ollama instance running locally. The model is loaded at runtime by
``llama-cpp-python`` (CPU by default, with optional GPU acceleration
on user installs).

Run once before the first PyInstaller build:

    python scripts/fetch_llm.py                  # default: 3B-Q4_K_M (~2 GB)
    python scripts/fetch_llm.py --size 1.5b      # smaller / faster fallback
    python scripts/fetch_llm.py --size 0.5b      # smallest, dev / tests only

The download is a single GGUF file; we resolve the right HF repo +
file name for each size below.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Each entry: HF repo id → exact GGUF file name (Q4_K_M quantization is
# the sweet spot of size vs quality for Qwen 2.5 instruct models).
_SIZE_TO_REPO: dict[str, tuple[str, str]] = {
    "3b": (
        "Qwen/Qwen2.5-3B-Instruct-GGUF",
        "qwen2.5-3b-instruct-q4_k_m.gguf",
    ),
    "1.5b": (
        "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "qwen2.5-1.5b-instruct-q4_k_m.gguf",
    ),
    "0.5b": (
        "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        "qwen2.5-0.5b-instruct-q4_k_m.gguf",
    ),
}

EMBEDDED_LLM_FILENAME = "describely-summary.gguf"
"""Stable on-disk name regardless of which size was downloaded.

The runtime looks for this exact filename under ``models/llm/`` so we
can swap quantizations / models without changing app/gui/model_manager.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--size",
        default="3b",
        choices=sorted(_SIZE_TO_REPO),
        help="Model size to download (default: 3b).",
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent / "models" / "llm"),
        help="Local directory the GGUF file will land in (default: ./models/llm).",
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

    repo_id, file_name = _SIZE_TO_REPO[args.size]
    target_dir = Path(args.root).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / EMBEDDED_LLM_FILENAME

    print(f"Downloading {repo_id} :: {file_name}")
    print(f"           → {target_path}")
    print("(this is a single file; may take a few minutes on slow connections)")

    downloaded = hf_hub_download(
        repo_id=repo_id,
        filename=file_name,
        local_dir=str(target_dir),
    )
    downloaded_path = Path(downloaded).resolve()

    # huggingface_hub honours the upstream filename; rename to our
    # stable name so PyInstaller and model_manager don't need to know
    # which size shipped this build.
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
