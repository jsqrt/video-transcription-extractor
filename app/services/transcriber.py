from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable, Protocol

from app.models.types import Transcript, TranscriptionError, Utterance


class TranscriptionProvider(Protocol):
    def transcribe(
        self,
        audio_path: Path,
        model: str,
        profile: str,
        language: str | None,
        timeout_sec: int,
        progress_callback: Callable[[float], None] | None = None,
        cancel_event: threading.Event | None = None,
        initial_prompt: str | None = None,
    ) -> str: ...


TIMESTAMPED_SPEAKER_LINE_RE = re.compile(
    r"^\[(?P<start>-?\d+(?:\.\d+)?)\s*->\s*(?P<end>-?\d+(?:\.\d+)?)\]\s*"
    r"\[(?P<speaker>[A-Za-z0-9_]+)\]\s*(?P<text>.+)$"
)
SPEAKER_LINE_RE = re.compile(r"^\[(?P<speaker>[A-Za-z0-9_]+)\]\s*(?P<text>.+)$")
WHITESPACE_RE = re.compile(r"\s+")

TRAILING_REPEAT_MIN_RUN = 30
TRAILING_REPEAT_MIN_TEXT_LEN = 10
TRAILING_REPEAT_KEEP = 2


class Transcriber:
    def __init__(
        self,
        provider: TranscriptionProvider,
        logger_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.provider = provider
        self._logger = logger_fn or (lambda message: None)
        # Populated by the last call to ``transcribe``. Surfaces Whisper's
        # trail-loop artefact (where a phrase is repeated until EOF) so
        # the caller can warn the user that audio in the loop region
        # was likely lost.
        self.last_trail_loop_trim: int = 0
        self.last_trail_loop_sample: str = ""

    def transcribe(
        self,
        audio_path: Path,
        model: str,
        profile: str,
        language: str | None,
        timeout_sec: int,
        progress_callback: Callable[[float], None] | None = None,
        cancel_event: threading.Event | None = None,
        initial_prompt: str | None = None,
    ) -> Transcript:
        self.last_trail_loop_trim = 0
        self.last_trail_loop_sample = ""
        raw_text = self.provider.transcribe(
            audio_path=audio_path,
            model=model,
            profile=profile,
            language=language,
            timeout_sec=timeout_sec,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            initial_prompt=initial_prompt,
        )
        # When the user forced a language, the providers leave
        # ``last_detected_language`` unset — we prefer the forced value
        # in that case so the downstream summarizer still gets a hint.
        detected_language = getattr(self.provider, "last_detected_language", None)
        if not detected_language and language:
            detected_language = str(language).strip().lower() or None
        detected_probability = getattr(
            self.provider, "last_detected_language_probability", None
        )
        return self._to_structured_transcript(
            raw_text,
            detected_language=detected_language,
            detected_probability=detected_probability,
        )

    def release(self) -> None:
        """Free GPU memory held by the underlying provider (if supported)."""
        if hasattr(self.provider, "release"):
            self.provider.release()

    def _to_structured_transcript(
        self,
        text: str,
        *,
        detected_language: str | None = None,
        detected_probability: float | None = None,
    ) -> Transcript:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        lines = self._trim_trailing_repeat_loop(lines)

        if not lines:
            raise TranscriptionError("Received empty transcription")

        utterances: list[Utterance] = []
        for line in lines:
            timestamped_match = TIMESTAMPED_SPEAKER_LINE_RE.match(line)
            if timestamped_match:
                speaker = timestamped_match.group("speaker").upper()
                speech = timestamped_match.group("text").strip()
                if not speech:
                    continue

                start_sec = float(timestamped_match.group("start"))
                end_sec = float(timestamped_match.group("end"))
                if end_sec < start_sec:
                    start_sec, end_sec = end_sec, start_sec

                utterances.append(
                    Utterance(
                        speaker=speaker,
                        text=speech,
                        start_sec=start_sec,
                        end_sec=end_sec,
                    )
                )
                continue

            match = SPEAKER_LINE_RE.match(line)
            if match:
                speaker = match.group("speaker").upper()
                speech = match.group("text").strip()
                if not speech:
                    continue
                utterances.append(Utterance(speaker=speaker, text=speech))
            else:
                utterances.append(Utterance(speaker="UNKNOWN_SPEAKER", text=line))

        if not utterances:
            raise TranscriptionError("Failed to parse utterances from transcription")

        return Transcript(
            utterances=tuple(utterances),
            detected_language=detected_language,
            detected_language_probability=detected_probability,
        )

    def _extract_line_text(self, line: str) -> str:
        timestamped_match = TIMESTAMPED_SPEAKER_LINE_RE.match(line)
        if timestamped_match:
            return timestamped_match.group("text").strip()

        match = SPEAKER_LINE_RE.match(line)
        if match:
            return match.group("text").strip()

        return line.strip()

    def _normalize_for_repeat_detection(self, line: str) -> str:
        plain = self._extract_line_text(line).lower()
        return WHITESPACE_RE.sub(" ", plain).strip()

    def _trim_trailing_repeat_loop(self, lines: list[str]) -> list[str]:
        if len(lines) < TRAILING_REPEAT_MIN_RUN:
            return lines

        normalized = [self._normalize_for_repeat_detection(line) for line in lines]
        last_text = normalized[-1]
        if len(last_text) < TRAILING_REPEAT_MIN_TEXT_LEN:
            return lines

        run_len = 1
        index = len(normalized) - 1
        while index > 0 and normalized[index - 1] == last_text:
            run_len += 1
            index -= 1

        if run_len < TRAILING_REPEAT_MIN_RUN:
            return lines

        keep_until = max(0, index + TRAILING_REPEAT_KEEP)
        trimmed = lines[:keep_until]
        if not trimmed:
            return lines

        dropped = len(lines) - len(trimmed)
        if dropped > 0:
            self.last_trail_loop_trim = dropped
            self.last_trail_loop_sample = last_text
            sample = last_text[:80]
            self._logger(
                f"Detected Whisper trail-loop: dropped {dropped} trailing "
                f"copies of {sample!r}. Audio content inside the loop "
                "region was not transcribed."
            )
        return trimmed
