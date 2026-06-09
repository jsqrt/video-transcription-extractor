"""Embedded LLM provider for summarization.

Loads a GGUF model via ``llama-cpp-python`` and exposes the same
``chat_json`` interface that :class:`OllamaClient` does, so the
``Summarizer`` does not care which backend produced the JSON.

This is the **bundled fallback** that runs when:

* Ollama is not installed / not reachable on ``127.0.0.1:11434``, AND
* the maintainer pre-seeded ``models/llm/describely-summary.gguf``
  before building (see ``scripts/fetch_llm.py``).

Imports of ``llama_cpp`` are deferred until first use so the rest of
the app (and the test suite) can run in environments where the C++
extension is not installed.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from app.models.types import (
    ModelNotFoundError,
    ProviderUnavailableError,
    SummarizationError,
)


def _log_backend_info() -> str:
    """Return a one-line description of which llama.cpp backend is active.

    The Windows shipping bundle uses Vulkan wheels (AMD/Intel/NVIDIA),
    macOS uses Metal, CPU is the fallback. We probe the installed
    package's compiled-in capabilities so support tickets can confirm
    the user's actual code path instead of guessing from the binary.
    """
    try:
        from llama_cpp import llama_cpp as _lc_native  # noqa: WPS433
    except Exception:
        return "llama_cpp native module not importable"

    flags: list[str] = []
    for name in ("GGML_USE_VULKAN", "GGML_USE_CUDA", "GGML_USE_METAL", "GGML_USE_HIPBLAS"):
        if getattr(_lc_native, name, None):
            flags.append(name.removeprefix("GGML_USE_").lower())
    if not flags:
        flags.append("cpu")
    return "llama.cpp backends compiled in: " + ", ".join(flags)


class LlamaCppClient:
    """Thin wrapper around ``llama_cpp.Llama.create_chat_completion``."""

    def __init__(
        self,
        model_path: Path,
        *,
        n_ctx: int = 8192,
        n_gpu_layers: int = -1,
        max_tokens: int = 2048,
    ) -> None:
        self._model_path = Path(model_path)
        self._n_ctx = n_ctx
        # ``-1`` asks llama.cpp to offload every layer it can to the
        # GPU. With a CPU-only build of llama-cpp-python the flag is
        # silently ignored.
        self._n_gpu_layers = n_gpu_layers
        self._max_tokens = max_tokens
        self._llama: Any = None
        self._load_lock = threading.Lock()

    # ---- Probes -----------------------------------------------------------------

    def is_available(self) -> bool:
        """True iff (a) the model file exists and (b) llama_cpp imports."""
        if not self._model_path.is_file():
            return False
        try:
            import llama_cpp  # noqa: F401 (import-only probe)
        except Exception:
            return False
        return True

    # ---- Lazy model load ---------------------------------------------------------

    def _load(self):
        if self._llama is not None:
            return self._llama
        with self._load_lock:
            if self._llama is not None:
                return self._llama
            try:
                from llama_cpp import Llama
            except ImportError as exc:
                raise ProviderUnavailableError(
                    "llama-cpp-python is not installed. The bundled "
                    "summarizer cannot run without it."
                ) from exc

            if not self._model_path.is_file():
                raise ModelNotFoundError(
                    f"Embedded LLM file missing: {self._model_path}. "
                    "Re-run scripts/fetch_llm.py before launching the GUI."
                )

            # Honour a debug override that flips llama.cpp's own banner
            # back on — useful when a support case needs to see exact
            # backend / device selection lines.
            verbose = os.environ.get("DESCRIBELY_LLAMA_VERBOSE") == "1"

            try:
                from app.gui.app_logger import log as _file_log
                _file_log(_log_backend_info())
            except Exception:
                pass

            self._llama = Llama(
                model_path=str(self._model_path),
                n_ctx=self._n_ctx,
                n_gpu_layers=self._n_gpu_layers,
                verbose=verbose,
                # Memory-map the GGUF (llama.cpp default): the OS pages the
                # weights in lazily on demand, keeping peak RAM low — which
                # matters on the 16 GB machines this bundle targets, right
                # after Whisper has run. (Forcing use_mmap=False would read
                # the whole file into RAM up front and risk an OOM there.)
                use_mmap=True,
            )
            return self._llama

    # ---- Public API -------------------------------------------------------------

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.0,
    ) -> str:
        """Free-form chat: returns the model's reply as plain text."""
        return self._complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            response_format=None,
        )

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        response_schema: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Call the bundled LLM and return parsed JSON.

        When ``response_schema`` is provided, uses llama.cpp's JSON-schema
        grammar constraint (``json_schema`` response format) so all required
        fields are guaranteed. Falls back to ``json_object`` for broad
        compatibility with older llama-cpp-python builds.
        """
        if response_schema is not None:
            fmt: dict = {"type": "json_schema", "json_schema": {"schema": response_schema}}
        else:
            fmt = {"type": "json_object"}

        content = self._complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            response_format=fmt,
        )

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise SummarizationError(
                f"Bundled LLM returned non-JSON content despite grammar: {exc}"
            ) from exc

        if not isinstance(parsed, dict):
            return {"value": parsed}
        return parsed

    def _complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        response_format: Optional[dict],
    ) -> str:
        """Single create_chat_completion call shared by chat / chat_json.

        Returns the raw assistant ``content`` string. ``response_format``
        is forwarded to llama-cpp when non-None so JSON-schema callers
        can constrain the grammar; ``chat`` leaves it unset for free-form
        markdown output.
        """
        llama = self._load()
        kwargs: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "top_p": 0.9,
            "max_tokens": self._max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        try:
            response = llama.create_chat_completion(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise SummarizationError(
                f"Bundled LLM failed during generation: {exc}"
            ) from exc

        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SummarizationError(
                "Bundled LLM returned an unexpected response shape."
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise SummarizationError("Bundled LLM returned empty content.")
        return content


def llama_cpp_available() -> bool:
    """Cheap module-level probe — useful for tests + summarizer wiring."""
    try:
        import llama_cpp  # noqa: F401
    except Exception:
        return False
    return True


__all__ = ["LlamaCppClient", "llama_cpp_available"]
