"""GPU vendor detection for the Windows runtime.

Used by :mod:`app.gui.worker` to pick the optimal Whisper backend:

* **NVIDIA** present  → faster-whisper (CUDA via CTranslate2 — fastest
  Whisper path on NVIDIA).
* **AMD / Intel** GPU → whisper.cpp Vulkan via pywhispercpp (only
  works in builds where the maintainer compiled pywhispercpp with
  ``CMAKE_ARGS=-DGGML_VULKAN=ON``).
* **No discrete GPU**  → faster-whisper CPU.

macOS doesn't need this — the worker always picks WhisperCppProvider
there (Metal on Apple Silicon, Accelerate on Intel).

Detection is best-effort: it shells out to ``nvidia-smi`` (which ships
with every NVIDIA driver) and times out fast. False negatives just
mean we skip the CUDA path and use Vulkan / CPU, which is harmless.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def has_nvidia_gpu() -> bool:
    """Return True iff an NVIDIA GPU + driver are usable on this host.

    Cached because the result cannot change without a driver
    reinstall, and a subprocess call per transcription job would be
    silly. Bypass via ``DESCRIBELY_FAKE_NVIDIA=1|0`` for testing.
    """
    import os

    forced = os.environ.get("DESCRIBELY_FAKE_NVIDIA")
    if forced in ("0", "1"):
        return forced == "1"

    if sys.platform == "darwin":
        return False

    exe = shutil.which("nvidia-smi")
    if exe is None:
        # Common Windows install location — some drivers don't add
        # nvidia-smi to PATH even though the executable is there.
        if sys.platform == "win32":
            from pathlib import Path
            candidate = Path(r"C:\Windows\System32\nvidia-smi.exe")
            if candidate.is_file():
                exe = str(candidate)
        if exe is None:
            return False

    try:
        result = subprocess.run(
            [exe, "-L"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # ``nvidia-smi -L`` returns lines like "GPU 0: NVIDIA GeForce ...".
    # No matching device → no output → no GPU.
    return result.returncode == 0 and "GPU" in (result.stdout or "")


def reset_cache() -> None:
    """Clear the cached probe — only useful in tests."""
    has_nvidia_gpu.cache_clear()


__all__ = ["has_nvidia_gpu", "reset_cache"]
