from __future__ import annotations

import re
from pathlib import Path
from typing import Callable
from typing import Protocol

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
    ) -> str:
        ...

SPEAKER_LINE_RE = re.compile(r"^\[(?P<speaker>[A-Za-z0-9_]+)\]\s*(?P<text>.+)$")


class Transcriber:
    def __init__(self, provider: TranscriptionProvider) -> None:
        self.provider = provider

    def transcribe(
        self,
        audio_path: Path,
        model: str,
        profile: str,
        language: str | None,
        timeout_sec: int,
        progress_callback: Callable[[float], None] | None = None,
    ) -> Transcript:
        raw_text = self.provider.transcribe(
            audio_path=audio_path,
            model=model,
            profile=profile,
            language=language,
            timeout_sec=timeout_sec,
            progress_callback=progress_callback,
        )
        return self._to_structured_transcript(raw_text)

    def _to_structured_transcript(self, text: str) -> Transcript:
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        if not lines:
            raise TranscriptionError("Received empty transcription")

        utterances: list[Utterance] = []
        for line in lines:
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

        return Transcript(utterances=utterances)
