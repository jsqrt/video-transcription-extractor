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
import os
from typing import Any, Optional

import httpx

from app.models.types import (
    ModelNotFoundError,
    ProviderUnavailableError,
    SummarizationError,
    SummarizationTimeoutError,
)


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back on default."""
    try:
        value = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# Bounds for the dynamically-sized context window.
#
# A fixed 4096 window silently truncated the START of a long transcript —
# Ollama drops oldest tokens once the prompt overflows num_ctx, so the model
# only saw the tail of the video and summarised that. A news transcript that
# opened with a shelling report and closed with a school segment came back as a
# summary about the school, the shelling missing entirely. We now size the
# window to the actual prompt length instead.
#
# FLOOR keeps short transcripts at the memory-cheap 4K that's safe on a 16 GB
# unified-memory Mac right after Whisper. CEIL caps growth so a very long video
# can't blow past available memory (the "model runner has unexpectedly stopped"
# OOM). DESCRIBELY_OLLAMA_NUM_CTX now overrides the CEIL (it used to pin the
# fixed window), so existing "give it more headroom" overrides still work.
_NUM_CTX_FLOOR = 4096
# 32K accommodates the full prompt for a 30-minute Ukrainian transcript
# (~36K chars ≈ 18K Cyrillic tokens after BPE). At the previous 16K cap
# Ollama was silently dropping ~half the transcript from the START,
# which the 7B summariser then could not recover — it would skip the
# first half of the news bulletin and start mid-way through. qwen2.5
# advertises 128K, so 32K is well within the model's capabilities; the
# only cost is memory pressure on smaller (≤16 GB) Macs, mitigated by
# DESCRIBELY_OLLAMA_NUM_CTX override.
_NUM_CTX_CEIL_DEFAULT = 32768

# Cyrillic tokenises far worse than English under BPE — roughly one token per
# ~2 characters vs ~4 for English. We assume the worse ratio so we never
# UNDER-size the window for Ukrainian/Russian (under-sizing is the bug we're
# fixing; over-sizing only costs a little memory).
_CHARS_PER_TOKEN = 2.0

# Bounds for the dynamically-sized reply budget (num_predict).
#
# The summary should compress the transcript ~10-15x in characters. A fixed
# num_predict cap was forcing 30-minute news reports to truncate mid-summary
# and the model would self-repeat to "fill" the cap when it sensed it didn't
# have room to finish properly. We now scale the reply budget to the prompt
# length: short clips get a modest cap (cheap, fast), long videos get enough
# headroom to actually finish the digest.
#
# At ~15x compression and 2 chars/token for Cyrillic, num_predict ≈
# prompt_chars / 15 / 2. Floor keeps low-content clips usable; ceil prevents
# runaway generation on hour-long inputs.
_NUM_PREDICT_FLOOR = 1024
# 6144 tokens ≈ 12 KB of Cyrillic markdown — enough headroom for the
# map-reduce reduce pass to write a full 10-14-paragraph digest for an
# hour-long transcript without truncating mid-sentence. The previous
# 4096 ceiling clipped the tail of qwen2.5:7b's reduce output on the
# 22-minute test bulletin ("...професії інженера Мехатроніка а").
_NUM_PREDICT_CEIL = 6144
# Compression target after the empirical sweep on test2.mp4 (22-minute
# Ukrainian news bulletin, 11 stories): 15x left the model truncating the
# last 3 stories. 10x gives ~2.5K-char summaries that fit 6-9 paragraphs
# for a half-hour video — matches the user's stated heuristic of "1
# paragraph per ~5 minutes of content".
_COMPRESSION_RATIO = 10.0


def _estimate_num_predict(prompt_chars: int) -> int:
    """Pick a reply-token budget that fits a ~10-15x compressed digest."""
    target_chars = prompt_chars / _COMPRESSION_RATIO
    needed = int(target_chars / _CHARS_PER_TOKEN)
    # Round up to the next 256 boundary for tidy sizes.
    needed = ((needed + 255) // 256) * 256
    return max(_NUM_PREDICT_FLOOR, min(needed, _NUM_PREDICT_CEIL))


def _estimate_num_ctx(prompt_chars: int, num_predict: int) -> int:
    """Pick a context window that fits the whole prompt plus its reply.

    ``prompt_chars`` is the combined length of the system + user messages.
    Result is clamped to ``[_NUM_CTX_FLOOR, ceil]`` where ``ceil`` is
    ``DESCRIBELY_OLLAMA_NUM_CTX`` if set, else ``_NUM_CTX_CEIL_DEFAULT``.
    """
    ceil = _env_int("DESCRIBELY_OLLAMA_NUM_CTX", _NUM_CTX_CEIL_DEFAULT)
    needed = int(prompt_chars / _CHARS_PER_TOKEN) + num_predict
    # Round up to the next 1024 boundary for tidy, cache-friendly sizes.
    needed = ((needed + 1023) // 1024) * 1024
    return max(_NUM_CTX_FLOOR, min(needed, ceil))


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

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.0,
        num_predict_floor: Optional[int] = None,
    ) -> str:
        """Free-form chat: returns the model's reply as a plain string.

        Used by the summarizer to get ready-to-render markdown back
        rather than a structured JSON payload. The same HTTP / error
        plumbing as ``chat_json`` is reused via ``_post_chat`` — only
        the format directive and the response parsing differ.

        ``num_predict`` is scaled to the prompt length so a 5-minute clip
        gets a tight budget and a 30-minute report gets enough room to
        finish all its paragraphs. A fixed cap caused long news reports
        to truncate mid-summary and the model self-repeated to fill it.

        ``num_predict_floor`` lifts the lower bound for this call only.
        The map-reduce summariser uses it for the reduce/refine phases:
        their PROMPTS are short (just the chunk drafts) but their REPLIES
        must be long (the full final digest), so the prompt-length-based
        budget would under-size the reply and clip it mid-sentence.
        """
        num_predict = _estimate_num_predict(
            prompt_chars=len(system_prompt) + len(user_prompt),
        )
        if num_predict_floor is not None:
            num_predict = max(num_predict, num_predict_floor)
        content = self._post_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            format_value=None,
            num_predict=num_predict,
        )
        return content

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
        content = self._post_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            format_value=format_value,
            num_predict=1024,
        )

        try:
            parsed_content = json.loads(content)
        except json.JSONDecodeError as exc:
            raise SummarizationError(
                f"Ollama returned non-JSON content despite format=json: {exc}"
            ) from exc

        if not isinstance(parsed_content, dict):
            return {"value": parsed_content}
        return parsed_content

    def _post_chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        format_value: Any,
        num_predict: int,
    ) -> str:
        """Single ``/api/chat`` call shared by ``chat`` and ``chat_json``.

        Returns the model's raw ``content`` string. Callers decide whether
        to feed that into a JSON parser (``chat_json``) or hand it back
        verbatim (``chat``). All of Ollama's error modes are normalised to
        the project's standard exception hierarchy here so callers don't
        each re-translate ``httpx`` exceptions.
        """
        payload = {
            "model": self.model,
            "stream": False,
            "options": {
                "temperature": temperature,
                "top_p": 0.9,
                # Anti-repetition controls. Smaller models (≤7B) on long
                # Ukrainian prompts collapse into "repeat the same paragraph
                # until num_predict runs out" without these. ``repeat_penalty``
                # divides logits of tokens seen in the recent window by this
                # factor before sampling; 1.15–1.2 is the band that breaks
                # paragraph-level loops without hurting fluency. ``repeat_last_n``
                # is the window size to watch — Ollama's default 64 is too short
                # to detect a 4-sentence paragraph being re-emitted, so we widen
                # it. ``presence_penalty`` adds a one-shot push to introduce new
                # tokens. The combination is the cheapest cure for the collapse
                # mode we observed on qwen2.5:3b summarising a 30-minute news
                # bulletin (the model would loop one paragraph 7+ times).
                # Kept at 1.1 (bottom of the band): raising it to 1.2 stopped
                # a loop but pushed the small model OFF its facts — it began
                # fabricating cities/dates/names and echoing stitch labels.
                # The loop that tempted the bump turned out to be an INPUT
                # artifact (utterances space-joined into run-on text shifted
                # the chunk boundaries); feeding newline-structured cleaned
                # text fixed it without touching this penalty. Fidelity wins:
                # an occasional loop is recoverable, confabulation is not.
                "repeat_penalty": 1.1,
                "repeat_last_n": 512,
                "presence_penalty": 0.0,
                # Context window, sized to the prompt so the whole transcript
                # fits (see _estimate_num_ctx). Short videos stay at the 4K
                # floor; long ones grow up to the ceil. Without this, Ollama
                # truncates the prompt's START and the summary silently loses
                # the opening of the video.
                "num_ctx": _estimate_num_ctx(
                    prompt_chars=len(system_prompt) + len(user_prompt),
                    num_predict=num_predict,
                ),
                # Cap generation: stops the model running away on a long
                # transcript — the single biggest lever on summarization
                # wall-clock time.
                "num_predict": num_predict,
            },
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if format_value is not None:
            payload["format"] = format_value

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
        return content
