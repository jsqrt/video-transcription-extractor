"""Diarization service wrapper using whisperx when available.

This module exposes a single function `diarize(wav_path)` which returns a
list of segments of the form dict(start: float, end: float, speaker: str).
The implementation is intentionally defensive: if `whisperx` is not
installed or its API is not available, we raise `ProviderUnavailableError`.
"""
from __future__ import annotations

from typing import List

from app.models.types import ProviderUnavailableError


def diarize(wav_path: str) -> List[dict]:
    """Run diarization on `wav_path` and return a list of segments.

    Each segment is a dict: {"start": float, "end": float, "speaker": str}.
    """
    try:
        import whisperx
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise ProviderUnavailableError(
            "whisperx is not installed or importable; install it to enable diarization"
        ) from exc

    # Best-effort API usage: whisperx exposes different helper functions
    # depending on version. Try common call patterns and provide a clear
    # error if none match.
    # 1) If whisperx exposes a high-level `diarize` function, prefer that.
    if hasattr(whisperx, "diarize"):
        raw = whisperx.diarize(wav_path)
        # Expect raw to be iterable of (start, end, speaker) or dicts.
        segments = []
        for item in raw:
            if isinstance(item, dict):
                s = float(item.get("start"))
                e = float(item.get("end"))
                sp = str(item.get("speaker") or item.get("label") or "Speaker")
            elif isinstance(item, (list, tuple)) and len(item) >= 3:
                s, e, sp = item[0], item[1], item[2]
            else:
                continue
            segments.append({"start": float(s), "end": float(e), "speaker": str(sp)})
        if segments:
            return segments

    # 2) Try a pipeline-style API (e.g. whisperx.DiarizationPipeline)
    if hasattr(whisperx, "DiarizationPipeline"):
        try:
            pipeline = whisperx.DiarizationPipeline()
            result = pipeline(wav_path)
            # Try to iterate result and extract segments
            segments = []
            for seg in result:
                # seg might be an object with .start/.end/.label
                s = getattr(seg, "start", None) or seg.get("start")
                e = getattr(seg, "end", None) or seg.get("end")
                lbl = getattr(seg, "label", None) or seg.get("label") or getattr(seg, "speaker", None) or seg.get("speaker")
                if s is None or e is None:
                    continue
                segments.append({"start": float(s), "end": float(e), "speaker": str(lbl)})
            if segments:
                return segments
        except Exception:
            # Fall through to helpful error below
            pass

    raise ProviderUnavailableError(
        "whisperx is installed but an expected diarization API was not found. "
        "Check your whisperx version or install a supported release."
    )
