"""PySide6 desktop GUI for the transcription pipeline.

Launched via ``python -m app.gui [FILE ...]``. The window opens with the
given files already in the queue and starts processing immediately.

The GUI intentionally exposes no options panel — defaults are baked in:

* profile = ``best``
* language = auto-detect (``None``)
* clean_mode = ``rule-based``
* summary mode = ``ollama`` (falls back to ``extractive`` if unreachable)
* output next to the source file
* both ``.clean.md`` and ``.summary.md`` are written
"""
