"""Pre-seed the tokenizer used to size summarizer chunks.

The summarizer splits long transcripts into chunks of roughly a fixed
token budget before summarising them (see app/services/tokenization.py).
To count tokens the same way the LLM does we ship a HuggingFace
``tokenizer.json``. We bundle the **Qwen2.5** tokenizer because qwen2.5:7b
is the fixed summary model (see SummaryOptions.ollama_model in
app/models/types.py); for other installed models it is only an estimate,
which is fine — chunk boundaries don't need bit-exact token counts, just
a stable, repeatable split. If the file is absent the runtime falls back
to a chars/token heuristic.

Run once on every build host before the first PyInstaller run:

    python scripts/fetch_summary_tokenizer.py

Output: ``models/tokenizer/tokenizer.json`` (~7 MB).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Qwen2.5's tokenizer.json on HuggingFace. The instruct repo carries the
# same tokenizer as the base; we use the 7B-Instruct repo as the canonical
# source. The tokenizer is identical across Qwen2.5 sizes (0.5B..72B).
_REPO_ID = "Qwen/Qwen2.5-7B-Instruct"
_FILENAME = "tokenizer.json"
_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "models" / "tokenizer"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=str(_DEFAULT_DIR),
        help="Directory tokenizer.json will land in (default: ./models/tokenizer).",
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

    target_dir = Path(args.root).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / _FILENAME

    print(f"Downloading {_REPO_ID} :: {_FILENAME}")
    print(f"           → {target_path}")

    downloaded = hf_hub_download(
        repo_id=_REPO_ID,
        filename=_FILENAME,
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

    size_kb = target_path.stat().st_size // 1024
    print(f"OK — {target_path} populated ({size_kb} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
