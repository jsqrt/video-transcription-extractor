# Summary evaluation set

A set of synthetic transcripts used to tune and compare summarization
strategies (single-pass vs flat-chunk+seam-stitch, window sizes,
overlap, temperature). NOT a unit test — it calls a live Ollama model.
Run with `python tests/summary_eval/run_eval.py`.

## Why synthetic

We need transcripts where the *ground-truth facts are known exactly*, so
coverage / hallucination can be scored automatically. Real articles don't
come with a labelled fact list. Each case carries:

* `transcript` — the input text (what the summarizer sees).
* `facts` — atomic facts that MUST appear in a faithful summary, each as
  a list of accept-markers (any marker present = fact covered).
* `absent` — plausible-but-false facts that must NOT appear (hallucination
  probes).
* `language` — expected output language code.

## Dimensions covered (cases/*.json)

The set spans the axes that broke earlier strategies:

| # | id                  | length | density | stresses                              |
|---|---------------------|--------|---------|---------------------------------------|
| 1 | short_single        | tiny   | low     | single chunk, base case               |
| 2 | short_dense_numbers | small  | high    | numbers must survive                  |
| 3 | news_multi_short    | medium | high    | many short stories (the test2 killer) |
| 4 | report_long_dense   | large  | high    | multi-chunk, dense facts              |
| 5 | interview_sparse    | medium | low     | lots of words, few facts              |
| 6 | boundary_straddle   | medium | mid     | one story split across a chunk seam   |
| 7 | english_doc         | medium | mid     | language stays English                |
| 8 | repeated_phrasing   | medium | mid     | same sentence recurs (slice bug probe)|
| 9 | names_heavy         | medium | high    | many named entities must survive      |
| 10| housekeeping_noise  | medium | mid     | greetings/CTAs must be dropped        |

## Scoring

`run_eval.py` reports per case: coverage (facts hit / total), numeric
coverage, hallucination hits (from `absent`), duplicate rate, paragraph
count, language match. It runs each case N times to expose variance.
