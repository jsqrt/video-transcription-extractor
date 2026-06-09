"""Regenerate the three sample artefacts for ``video-sample/1.mp4``.

Reads the ground-truth utterances from whichever of these exists:

    video-sample/1.raw.txt
    video-sample/1.transcript.txt  (legacy)
    video-sample/1.clean.md        (new)

Runs rule-based cleanup, rebuilds chapters, produces an extractive
summary, and writes:

    video-sample/1.raw.txt
    video-sample/1.clean.md
    video-sample/1.summary.md

Intentionally simple — no argparse, no options. Run from project root:

    python scripts/regenerate_sample.py
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.cleanup import rule_based_cleanup
from app.services.raw_writer import RawTranscriptWriter
from app.services.transcriber import Transcriber
from app.services.writer import CleanTranscriptWriter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = PROJECT_ROOT / "video-sample"
SAMPLE_VIDEO = SAMPLE_DIR / "1.mp4"

SPEAKER_LINE = re.compile(r"^\[(?P<speaker>[A-Z_0-9]+)\]\s*(?P<text>.+)$")
TIMECODE_LINE = re.compile(
    r"^\[(?P<tc>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?P<text>.+)$"
)
# Matches the ``.raw.txt`` placeholder timecode ``[--:--]`` the raw
# writer emits when timing info is missing.
PLACEHOLDER_TC_LINE = re.compile(r"^\[--:--\]\s*(?P<text>.+)$")


class _FakeProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    def transcribe(self, *args, **kwargs) -> str:
        return self._text


def _pick_source() -> Path | None:
    # Prefer the original legacy transcript to the generated ``.raw.txt``
    # so repeated runs stay idempotent. (The generated raw file has
    # ``[MM:SS]`` prefixes that _load_raw is expected to strip, but it's
    # cleaner to re-read the ground truth when it's around.)
    for candidate in (
        SAMPLE_DIR / "1.transcript.txt",
        SAMPLE_DIR / "1.clean.md",
        SAMPLE_DIR / "1.raw.txt",
    ):
        if candidate.exists():
            return candidate
    return None


def _load_raw(path: Path) -> str:
    """Render the sample file as provider-shaped line-per-utterance text.

    We fabricate ascending timecodes (3 seconds per utterance) so the
    generated ``.raw.txt`` has realistic ``[MM:SS]`` prefixes. Real
    Whisper output carries per-segment timestamps; the legacy sample
    file does not, so we synthesize them here.
    """
    payload_lines: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(">") or line.startswith("-"):
            continue
        m_placeholder = PLACEHOLDER_TC_LINE.match(line)
        if m_placeholder:
            payload_lines.append(("SPEAKER_1", m_placeholder.group("text")))
            continue
        m_speaker = SPEAKER_LINE.match(line)
        if m_speaker:
            payload_lines.append(
                (m_speaker.group("speaker"), m_speaker.group("text"))
            )
            continue
        m_tc = TIMECODE_LINE.match(line)
        if m_tc:
            payload_lines.append(("SPEAKER_1", m_tc.group("text")))
            continue
        payload_lines.append(("SPEAKER_1", line))

    seconds_per_line = 3.0
    assembled: list[str] = []
    for index, (speaker, text) in enumerate(payload_lines):
        start = index * seconds_per_line
        end = start + seconds_per_line
        assembled.append(f"[{start:.2f} -> {end:.2f}] [{speaker}] {text}")
    return "\n".join(assembled)


def main() -> int:
    source = _pick_source()
    if source is None:
        print(f"Missing a source transcript in {SAMPLE_DIR}.")
        return 1

    raw_text = _load_raw(source)
    transcriber = Transcriber(provider=_FakeProvider(raw_text))
    transcript = transcriber.transcribe(
        audio_path=SAMPLE_VIDEO,
        model="large-v3",
        profile="best",
        language="uk",
        timeout_sec=0,
    )

    cleaned = rule_based_cleanup(transcript)

    raw_path = RawTranscriptWriter().write(
        source_video=SAMPLE_VIDEO,
        transcript=transcript,
        output_dir=SAMPLE_DIR,
    )
    # Summary regeneration was removed alongside the extractive
    # summarizer: the LLM-only summary path needs Ollama and is
    # nondeterministic, so it's a poor fit for a fixture-regeneration
    # script. Run the GUI / CLI manually if you need a fresh sample
    # summary file.
    clean_path = CleanTranscriptWriter().write(
        source_video=SAMPLE_VIDEO,
        transcript=cleaned,
        output_dir=SAMPLE_DIR,
        clean_mode="raw",  # already cleaned above
    )

    print(f"Wrote: {raw_path}")
    print(f"Wrote: {clean_path}")
    print(f"Raw utterances: {len(transcript.utterances)}")
    print(f"Clean utterances: {len(cleaned.utterances)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
