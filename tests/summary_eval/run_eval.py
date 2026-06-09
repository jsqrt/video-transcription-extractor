"""Evaluate the summarizer against the synthetic case set.

Runs the real Summarizer (live Ollama) on each tests/summary_eval/cases/*.json
and scores the output against that case's labelled facts. Each case is run
``--runs`` times so we can see variance, not just a lucky single shot.

Usage:
    python tests/summary_eval/run_eval.py                  # all cases, qwen2.5:7b, 3 runs
    python tests/summary_eval/run_eval.py --runs 1 --model qwen2.5:3b
    python tests/summary_eval/run_eval.py --case news_multi_short

Metrics per run:
    cov   facts covered / total          (higher better)
    num   numeric facts covered          (subset of cov that look numeric)
    hall  hallucination probes hit        (lower better, target 0)
    dup   5-gram repetition rate          (lower better)
    par   paragraph count
    lang  output language matches expected (ok/BAD)
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.models.types import SummaryOptions, Transcript, Utterance
from app.providers.ollama_provider import OllamaClient
from app.services.summarizer import Summarizer

CASES_DIR = Path(__file__).resolve().parent / "cases"

_NUMERIC_RE = re.compile(r"\d")
_CYRILLIC_RE = re.compile(r"[а-яіїєґ]", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower(), flags=re.UNICODE)


def repetition_rate(text: str, n: int = 5) -> float:
    tokens = tokenize(text)
    if len(tokens) < n:
        return 0.0
    grams = [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]
    counts = Counter(grams)
    repeated = sum(c for c in counts.values() if c > 1)
    return repeated / len(grams)


def fact_covered(text: str, markers: list[str]) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in markers)


def is_numeric_fact(markers: list[str]) -> bool:
    return any(_NUMERIC_RE.search(m) for m in markers)


def language_ok(text: str, expected: str) -> bool:
    cyr = len(_CYRILLIC_RE.findall(text))
    total = len(re.findall(r"[a-zа-яіїєґ]", text, re.IGNORECASE)) or 1
    ratio = cyr / total
    if expected == "uk":
        return ratio > 0.6          # mostly Cyrillic
    if expected == "en":
        return ratio < 0.2          # mostly Latin
    return True


def score_summary(summary: str, case: dict) -> dict:
    facts = case["facts"]
    covered = [f for f in facts if fact_covered(summary, f)]
    num_facts = [f for f in facts if is_numeric_fact(f)]
    num_covered = [f for f in num_facts if fact_covered(summary, f)]
    hall = [a for a in case.get("absent", []) if fact_covered(summary, a)]
    paragraphs = [p for p in summary.split("\n\n") if p.strip()]
    return {
        "cov": (len(covered), len(facts)),
        "num": (len(num_covered), len(num_facts)),
        "hall": len(hall),
        "hall_list": hall,
        "dup": repetition_rate(summary),
        "par": len(paragraphs),
        "lang_ok": language_ok(summary, case["language"]),
        "chars": len(summary),
    }


def make_transcript(text: str) -> Transcript:
    # The summarizer joins utterance texts with spaces, so a single
    # utterance carrying the whole transcript reproduces the input exactly.
    return Transcript(
        utterances=(Utterance(speaker="S", text=text, start_sec=0.0, end_sec=1.0),),
        detected_language=None,
    )


def load_cases(only: str | None) -> list[dict]:
    cases = []
    for path in sorted(CASES_DIR.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        if only and case["id"] != only:
            continue
        cases.append(case)
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--case", default=None, help="run a single case id")
    args = ap.parse_args()

    cases = load_cases(args.case)
    if not cases:
        print("no cases found", file=sys.stderr)
        return 1

    llm = OllamaClient(model=args.model, timeout_sec=600)
    summarizer = Summarizer(options=SummaryOptions(mode="ollama"), llm_client=llm)

    print(f"model={args.model} runs={args.runs} cases={len(cases)}\n")
    header = f"{'case':22} {'cov':>9} {'num':>7} {'hall':>4} {'dup%':>6} {'par':>4} {'lang':>5}"
    grand_cov = []
    for case in cases:
        transcript = make_transcript(case["transcript"])
        rows = []
        for _ in range(args.runs):
            summary = summarizer.summarize(transcript=transcript, language=case["language"]) or ""
            rows.append(score_summary(summary, case))
        print(header)
        for r in rows:
            cov = r["cov"][0] / max(r["cov"][1], 1)
            grand_cov.append(cov)
            num = f"{r['num'][0]}/{r['num'][1]}"
            flag = "" if r["hall"] == 0 else f" !{r['hall_list']}"
            lang = "ok" if r["lang_ok"] else "BAD"
            print(f"{case['id']:22} {r['cov'][0]:>3}/{r['cov'][1]:<3}{cov:>5.0%} "
                  f"{num:>7} {r['hall']:>4} {r['dup']*100:>5.1f} {r['par']:>4} {lang:>5}{flag}")
        # variance across runs for the headline metric
        covs = [r["cov"][0] / max(r["cov"][1], 1) for r in rows]
        if len(covs) > 1:
            print(f"{'  ↳ coverage spread':22} min={min(covs):.0%} max={max(covs):.0%} "
                  f"stdev={statistics.pstdev(covs):.2f}")
        print()

    print(f"GRAND mean coverage: {statistics.mean(grand_cov):.1%} "
          f"over {len(grand_cov)} runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
