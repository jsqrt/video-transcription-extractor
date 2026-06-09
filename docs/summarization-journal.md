# Summarization design notes

How the local summary pipeline (`app/services/summarizer.py`) is wired,
and the reasoning behind the current approach.

## Current approach

The summarizer turns a cleaned transcript into a digest with no stitch and
no prev-context:

1. **Chunking** (`app/services/chunking.py`): split on sentence boundaries,
   greedily pack whole sentences up to **450 tokens** (target == min == 450,
   overlap 0). A sentence is never split mid-way.
2. **Stage 1 — standalone per chunk**: each chunk is summarised on its own.
   The model sees only its own chunk (no previous-chunk context).
3. **Stitch — plain concatenation**: per-chunk summaries are joined with a
   blank-line seam. No fold-merge, no re-summarisation.
4. **Prompt — minimal**: a short system prompt + the raw chunk as the user
   message, with no FORMAT/length block. System prompt rules: restate in
   fewer words without losing facts; skip factless sentences; split
   fact-dense sentences; output is ONLY the finished text (no preamble,
   commentary, or explanations).

`prev-context` and `fold-merge stitch` remain in the code but commented out
for reference.

### Why this shape

- **Fold-merge stitch was the lossy stage.** Stage-1 reliably captured the
  facts, but a fold-merge re-summarisation pass was high-variance: it
  randomly dropped whole items and occasionally fabricated content. Plain
  concatenation of the stage-1 summaries is stable.
- **Chunk size is a minor knob.** 450 vs 600 vs 750 tokens only moves where
  loss happens (boundary vs in-chunk); it does not fix it.
- **A heavy prompt inflates tiny chunks.** A lone ~1-sentence tail chunk gets
  blown up into an invented paragraph when the prompt implies "produce a
  paragraph". The minimal prompt above keeps such chunks at ~1.0x size with
  no fabrication.

### Model

- **qwen2.5:3b** — fine on clean dense prose (e.g. English news), but
  unreliable on spontaneous Ukrainian (hallucinated domain content, role
  breaks, markdown lists, garbled morphology).
- **qwen2.5:7b** — fixes the above on the same Ukrainian input and is more
  complete on English. Cost: ~4.7 GB vs ~1.9 GB, slower, more RAM (OOM risk
  on 16 GB machines after Whisper).
- Conclusion: **7b is the quality bar** for mixed/Ukrainian content; 3b is
  the lightweight fallback.

## Tuning env vars

- `DESCRIBELY_CHUNK_TOKENS` / `DESCRIBELY_MIN_CHUNK_TOKENS` — chunk target/min
  (default 450/450).
- `DESCRIBELY_CHUNK_OVERLAP` — chunk overlap (default 0).
- `DESCRIBELY_DUMP_CHUNKS=<path>` — dump raw per-chunk stage-1 summaries for
  inspection.

## Known limitations

- Occasional language drift (Russian/Chinese) on noisy spontaneous Ukrainian.
  The reliable cure is an explicit "respond only in Ukrainian" language anchor
  in the prompt; sampling tweaks only relocate the drift. The anchor is not
  yet added.
- 3b hallucinates on spontaneous Ukrainian; prefer 7b there.
- Minor number/wording drift on some facts (e.g. "early May" rendered as
  "May 5").
