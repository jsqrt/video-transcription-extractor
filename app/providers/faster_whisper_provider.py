from __future__ import annotations

import os
import sys
from pathlib import Path

from app.models.types import ModelNotFoundError, TranscriptionError


class FasterWhisperProvider:
    def __init__(
        self,
        allow_online_model_download: bool = False,
        model_cache_dir: Path | None = None,
    ) -> None:
        self._model_cache: dict[str, object] = {}
        self.allow_online_model_download = allow_online_model_download
        self.model_cache_dir = model_cache_dir

    def _configure_windows_cuda_runtime(self) -> None:
        if os.name != "nt":
            return

        candidates: list[Path] = []

        cuda_path = os.environ.get("CUDA_PATH")
        if cuda_path:
            candidates.append(Path(cuda_path) / "bin")

        site_packages = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
        if site_packages.exists():
            for sub in site_packages.iterdir():
                bin_dir = sub / "bin"
                if bin_dir.exists():
                    candidates.append(bin_dir)

        seen: set[str] = set()
        for candidate in candidates:
            resolved = str(candidate.resolve())
            lowered = resolved.lower()
            if lowered in seen:
                continue
            seen.add(lowered)

            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(resolved)
                except OSError:
                    pass

            path_parts = os.environ.get("PATH", "").split(os.pathsep)
            if resolved not in path_parts:
                os.environ["PATH"] = resolved + os.pathsep + os.environ.get("PATH", "")

    def _load_model(self, model_name: str, prefer_gpu: bool = True):
        cache_key = f"{model_name}:{'gpu' if prefer_gpu else 'cpu'}"
        if cache_key in self._model_cache:
            return self._model_cache[cache_key]

        try:
            from faster_whisper import WhisperModel  # pylint: disable=import-outside-toplevel
        except Exception as exc:  # pragma: no cover
            raise TranscriptionError(
                "faster-whisper is not installed. Install dependencies: pip install -r requirements.txt"
            ) from exc

        self._configure_windows_cuda_runtime()

        attempts = [("cuda", "float16"), ("cpu", "int8")]
        if not prefer_gpu:
            attempts = [("cpu", "int8")]

        last_exc: Exception | None = None
        for device, compute_type in attempts:
            try:
                model = WhisperModel(
                    model_name,
                    device=device,
                    compute_type=compute_type,
                    download_root=str(self.model_cache_dir) if self.model_cache_dir else None,
                    local_files_only=not self.allow_online_model_download,
                )
                self._model_cache[cache_key] = model
                return model
            except Exception as exc:  # pragma: no cover
                last_exc = exc

        message = str(last_exc) if last_exc else "unknown error"
        if not self.allow_online_model_download:
            raise ModelNotFoundError(
                "Whisper model is not available locally in offline mode. "
                "Set --model-cache-dir to a local cache path "
                "or pre-seed the model cache offline."
            ) from last_exc

        if "not found" in message.lower() or "repository" in message.lower():
            raise ModelNotFoundError(
                f"Whisper model '{model_name}' was not found. Try: tiny, base, small, medium, large-v3"
            ) from last_exc

        raise TranscriptionError(f"Failed to load Whisper model: {message}") from last_exc

    def transcribe(
        self,
        audio_path: Path,
        model: str,
        profile: str,
        language: str | None,
        timeout_sec: int,
    ) -> str:
        del timeout_sec  # faster-whisper does not provide a hard timeout per call.

        normalized_profile = (profile or "best").lower()
        transcribe_kwargs: dict[str, object] = {
            "language": language,
            "vad_filter": True,
        }
        if normalized_profile == "fast":
            transcribe_kwargs.update({"beam_size": 2, "best_of": 2, "temperature": 0.0})
        else:
            transcribe_kwargs.update({"beam_size": 5, "best_of": 5, "temperature": 0.0})

        try:
            whisper_model = self._load_model(model_name=model, prefer_gpu=True)
            segments, _info = whisper_model.transcribe(
                str(audio_path),
                **transcribe_kwargs,
            )

            lines: list[str] = []
            for segment in segments:
                text = (segment.text or "").strip()
                if text:
                    lines.append(f"[UNKNOWN_SPEAKER] {text}")
        except Exception as exc:
            message = str(exc).lower()
            if "cublas64_12.dll" in message or "cudnn" in message or "cuda" in message:
                whisper_model = self._load_model(model_name=model, prefer_gpu=False)
                try:
                    segments, _info = whisper_model.transcribe(
                        str(audio_path),
                        **transcribe_kwargs,
                    )
                    lines = []
                    for segment in segments:
                        text = (segment.text or "").strip()
                        if text:
                            lines.append(f"[UNKNOWN_SPEAKER] {text}")
                except Exception as cpu_exc:
                    raise TranscriptionError(
                        f"Whisper failed during CPU fallback: {cpu_exc}"
                    ) from cpu_exc
            else:
                raise TranscriptionError(f"Whisper transcription failed: {exc}") from exc

        if not lines:
            raise TranscriptionError("Whisper returned empty transcription")

        return "\n".join(lines)
