# video-transcription-extractor

Minimal local CLI for video transcription.

## Summary

- Local, offline-first CLI for video-to-text transcription.
- Processes a single file or a whole directory.
- Uses `faster-whisper` with GPU support and `fast`/`best` profiles.
- Saves output as `<video_name>.transcript.txt`.

## Idea

- Make transcription simple: one command, predictable output.
- Keep data private: strict network isolation by default.
- Balance speed and quality with clear profile choices.
- Stay practical for Windows workflows and local processing.

## Install

```bash
python -m pip install -r requirements.txt
```

## Fast (GPU, lower quality)

```bash
python -m app transcribe --input "./video-sample/1.mp4" --backend faster-whisper --profile fast --language uk --verbose
```

## Best (GPU, higher quality)

```bash
python -m app transcribe --input "./video-sample/1.mp4" --backend faster-whisper --profile best --language uk --verbose
```

## Directory mode

```bash
python -m app transcribe --input "D:\\Videos" --ext "mp4,mov,mkv" --output-dir "D:\\Videos\\transcripts" --profile best --language uk
```

## Notes

- Network isolation is enabled by default (external internet is blocked).
- Only local model cache is used in offline mode.
- Output file format: `<video_name>.transcript.txt`.

## Pre-push Checklist

- `python -m app transcribe --help` works without errors.
- No secrets in tracked files (`.env`, keys, tokens, certificates).
- No `__pycache__` or other generated artifacts are tracked.
- README commands are up to date.
