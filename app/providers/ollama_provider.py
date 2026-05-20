"""Ollama client used for local, GPU-accelerated text summarization.

Uses Ollama's ``format: "json"`` parameter for reliable structured output.
This is the modern way to get JSON from a local LLM: the sampling layer
is constrained so the model can only emit valid JSON, which removes the
brittle "hope the model formatted it right" step.

Works with any model Ollama can serve — llama3.1, qwen2.5, gemma2,
mistral, etc. Pick a model that handles your transcript language well:
``qwen2.5:7b`` and ``llama3.1:8b`` are both solid for Ukrainian and
English at 16 GB VRAM.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from app.models.types import (
    ModelNotFoundError,
    ProviderUnavailableError,
    SummarizationError,
    SummarizationTimeoutError,
)


class OllamaClient:
    """Thin wrapper around a local Ollama ``/api/chat`` endpoint."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "llama3.1:8b",
        timeout_sec: int = 180,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec

    # ---- Probes -----------------------------------------------------------------

    def is_available(self) -> bool:
        try:
            with httpx.Client(base_url=self.base_url, timeout=3.0) as client:
                response = client.get("/api/tags")
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    def list_models(self) -> list[str]:
        """Return names of models currently installed in Ollama (e.g. 'gemma3:4b').

        Returns an empty list when the server is unreachable or the response is
        malformed.
        """
        try:
            with httpx.Client(base_url=self.base_url, timeout=3.0) as client:
                response = client.get("/api/tags")
                if response.status_code != 200:
                    return []
                data = response.json()
                models = data.get("models") or []
                return [m["name"] for m in models if isinstance(m, dict) and "name" in m]
        except Exception:
            return []

    def model_is_installed(self) -> bool:
        """True iff ``self.model`` appears in the Ollama model list."""
        installed = self.list_models()
        if not installed:
            return False
        # Exact match first; then prefix match (e.g. "gemma3:4b" in "gemma3:4b-instruct")
        if self.model in installed:
            return True
        return any(name.startswith(self.model.split(":")[0] + ":") for name in installed)

    # ---- Public API -------------------------------------------------------------

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        response_schema: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Call ``/api/chat`` with structured JSON output and return the parsed dict.

        When ``response_schema`` is a JSON Schema dict, Ollama 0.4+ constrains
        generation to that exact schema (all ``required`` fields are guaranteed).
        Falls back to ``format="json"`` when no schema is provided.

        Raises on network / protocol / model errors. Always returns a dict
        when it returns. If the model emits a JSON scalar or array, we wrap
        it under ``{"value": ...}`` so callers have a uniform shape.
        """
        format_value: Any = response_schema if response_schema is not None else "json"
        payload = {
            "model": self.model,
            "stream": False,
            "format": format_value,
            "options": {"temperature": temperature, "top_p": 0.9},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout_sec) as client:
                response = client.post("/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(
                "Ollama is unavailable. Start it with `ollama serve` "
                "and ensure it listens on 127.0.0.1:11434."
            ) from exc
        except httpx.TimeoutException as exc:
            raise SummarizationTimeoutError(
                f"Ollama call timed out after {self.timeout_sec}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise SummarizationError(f"Ollama HTTP error: {exc}") from exc

        if response.status_code == 404:
            raise ModelNotFoundError(
                f"Model '{self.model}' was not found in Ollama. "
                f"Run: ollama pull {self.model}"
            )

        if response.status_code >= 400:
            body = response.text.strip()
            try:
                parsed = json.loads(body)
                message = parsed.get("error") or parsed.get("message") or body
            except json.JSONDecodeError:
                message = body
            if "model" in message.lower() and "not found" in message.lower():
                raise ModelNotFoundError(
                    f"Model '{self.model}' was not found in Ollama. "
                    f"Run: ollama pull {self.model}"
                )
            raise SummarizationError(f"Ollama returned an error: {message}")

        data = response.json()
        message = data.get("message")
        if not isinstance(message, dict):
            raise SummarizationError("Invalid Ollama response: missing message field")

        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise SummarizationError("Ollama returned empty content")

        try:
            parsed_content = json.loads(content)
        except json.JSONDecodeError as exc:
            raise SummarizationError(
                f"Ollama returned non-JSON content despite format=json: {exc}"
            ) from exc

        if not isinstance(parsed_content, dict):
            return {"value": parsed_content}
        return parsed_content
