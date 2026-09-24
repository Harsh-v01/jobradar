"""Benchmark candidate Cursor models on a fixed, hand-labelled set of jobs.

The point is to choose a scoring model on evidence. Each candidate scores the
same postings; we report how far it lands from the expected band, how often it
returns usable JSON, and how long a batch takes.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .cursor import CursorError, CursorScorer
from .main import ROOT, get_profile, get_scorer, load_config

# (title, company, location, description, expected band) - deliberately spread
# from obvious-yes to obvious-no so a weak model's failures are visible.
CASES = [
    ("Junior Full Stack Developer", "Acme", "Remote",
     "React, Node.js, MongoDB. 0-2 years. Build product features.", (7, 10)),
    ("Software Engineer, Backend", "Acme", "Remote (India)",
     "Python, FastAPI, Postgres. 1-3 years experience.", (6, 10)),
    ("Frontend Engineer", "Acme", "Bangalore",
     "React, TypeScript, CSS. Early career welcome.", (6, 10)),
    ("Machine Learning Engineer", "Acme", "Remote",
     "TensorFlow, model training, Python. 2+ years.", (4, 8)),
    ("Senior Staff Engineer", "Acme", "San Francisco (onsite)",
     "Lead architecture across teams. 10+ years required.", (0, 3)),
    ("VP of Engineering", "Acme", "New York (onsite)",
     "Lead 200 engineers. 15+ years, executive experience.", (0, 2)),
    ("Executive Chef", "Acme", "Paris",
     "Menu design, kitchen management, culinary degree.", (0, 1)),
    ("Registered Nurse", "Acme", "London",
     "Patient care, nursing licence required.", (0, 1)),
]

CANDIDATES = [
    ("gemini-3.8-flash", {"reasoning_effort": "low"}),
    ("gpt-5.4-mini", {"reasoning": "none"}),
    ("gpt-5.4-nano", {"reasoning": "none"}),
    ("claude-haiku-4-5", {"thinking": "false"}),
    ("composer-2.5", {"fast": "true"}),
]


def jobs_payload() -> list[dict]:
    return [
        {"id": f"b{i}", "company": c, "title": t, "location": loc, "description": d}
        for i, (t, c, loc, d, _) in enumerate(CASES)
    ]


def evaluate(scorer, profile) -> dict:
    jobs = jobs_payload()
    started = time.time()
    got = scorer.score_batch(profile, jobs)
    elapsed = time.time() - started

    returned = len(got)
    off_by = []
    for i, (_, _, _, _, (lo, hi)) in enumerate(CASES):
        res = got.get(f"b{i}")
        if res is None:
            continue
        score = res[0]
        off_by.append(0 if lo <= score <= hi else min(abs(score - lo), abs(score - hi)))
    has_breakdown = sum(1 for v in got.values() if any(v[2].values()))
    return {
        "returned": returned,
        "of": len(CASES),
        "in_band": sum(1 for d in off_by if d == 0),
        "mean_miss": round(sum(off_by) / len(off_by), 2) if off_by else None,
        "breakdowns": has_breakdown,
        "seconds": round(elapsed, 1),
    }


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="jobradar.bench")
    p.add_argument("--models", nargs="*", help="model ids to test (default: the shortlist)")
    args = p.parse_args(argv)

    cfg = load_config(ROOT / "config.yaml")
    profile = get_profile(cfg, get_scorer(cfg))

    picks = ([(m, {}) for m in args.models] if args.models else CANDIDATES)
    print(f"{'model':<20} {'ret':>5} {'in-band':>8} {'miss':>6} {'bd':>4} {'secs':>6}")
    print("-" * 54)
    best = None
    for model, params in picks:
        try:
            r = evaluate(CursorScorer(model, params), profile)
        except CursorError as e:
            print(f"{model:<20} FAILED: {str(e)[:60]}")
            continue
        print(f"{model:<20} {r['returned']:>3}/{r['of']:<2} {r['in_band']:>6}/{r['of']:<2} "
              f"{str(r['mean_miss']):>6} {r['breakdowns']:>4} {r['seconds']:>6}")
        key = (r["in_band"], -(r["mean_miss"] or 99), -r["seconds"])
        if best is None or key > best[0]:
            best = (key, model, params)

    if best:
        _, model, params = best
        print(f"\nBest on these cases: {model} {params}")
        print("Set it in config.yaml under llm.model / llm.model_params.")
    else:
        print("\nNo model completed. Check the two Cursor account settings.")


if __name__ == "__main__":
    main()
