from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.models.types import (
    ModelNotFoundError,
    ProviderUnavailableError,
    TranscriptionError,
    TranscriptionTimeoutError,
)


class OllamaProvider:
    def __init__(self, base_url: str = "http://127.0.0.1:11434") -> None:
        self.base_url = base_url.rstrip("/")

    def _extract_content_text(self, message: dict) -> str:
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts).strip()
        return ""

    def transcribe(
        self,
        audio_path: Path,
        model: str,
        profile: str,
        language: str | None,
        timeout_sec: int,
    ) -> str:
        del profile
        system_prompt = (
            "You are an audio transcription assistant. "
            "Return ONLY plain text transcript with one utterance per line. "
            "Prefix each line with [SPEAKER_1], [SPEAKER_2], ... when possible. "
            "If speaker is unknown, use [UNKNOWN_SPEAKER]."
        )
        user_prompt = (
            f"Transcribe the attached audio file in language '{language or 'auto'}'. "
            "Keep punctuation and do not add explanations."
        )

        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": user_prompt,
                    "audio": [str(audio_path)],
                },
            ],
        }

        try:
            with httpx.Client(base_url=self.base_url, timeout=timeout_sec) as client:
                response = client.post("/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(
                "Ollama is unavailable. Check that the service is running."
            ) from exc
        except httpx.TimeoutException as exc:
            raise TranscriptionTimeoutError(
                f"Request to Ollama timed out ({timeout_sec} seconds)."
            ) from exc
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"Ollama HTTP error: {exc}") from exc

        if response.status_code == 404:
            raise ModelNotFoundError(
                f"Model '{model}' was not found in Ollama. Run: ollama pull {model}"
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
                    f"Model '{model}' was not found in Ollama. Run: ollama pull {model}"
                )
            raise TranscriptionError(f"Ollama returned an error: {message}")

        data = response.json()
        message = data.get("message")
        if not isinstance(message, dict):
            raise TranscriptionError("Invalid Ollama response: missing message field")

        text = self._extract_content_text(message)
        if not text:
            raise TranscriptionError("Ollama returned empty transcription")

        return text
