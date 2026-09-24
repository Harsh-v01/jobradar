"""Shared batch-scoring prompt and parser.

Three things drive the token bill, and this module addresses all three:

* **Per-call overhead.** Providers prepend their own preamble (xkiro adds ~550
  tokens), so one request per job would pay that thousands of times. Batching
  amortises it.
* **Output tokens.** They are billed like input and there is no cache discount.
  Repeating `"skills":`, `"seniority":` and friends for every job is pure
  waste, so replies are compact positional arrays, not objects.
* **The repeated prefix.** The instructions and the candidate profile are
  identical across every batch of a run. They go in a separate system message
  so providers that cache prompt prefixes can charge a tenth for them.
"""
from __future__ import annotations

import json
import re

KEYS = ("skills", "seniority", "location", "domain", "recency", "reach")

# Static across a whole run: identical bytes every batch, so it can be cached.
SYSTEM = """You score job postings for one candidate.

CANDIDATE:
{profile}

For each job output one array, positionally:
[ref, score, skills, seniority, location, domain, recency, reach, "reason"]

score 0-10. The six middle numbers are 0-100. reason is at most 8 words.

score 10 = perfect fit, 0 = wrong field.
Seniority is a hard gate, not a tiebreaker: a title containing Senior, Sr,
Staff, Principal, Lead, Manager, Head, Director or VP, or a stated requirement
of more years than the candidate has, scores at most 3 however well the skills
match. Set location at most 30 when the role is onsite somewhere they cannot
work.

Reply with ONLY a JSON array of these arrays. No prose, no keys, no markdown."""

USER = """Jobs, one per line as ref|company|title|location|description:
{jobs}"""


def build_messages(profile: str, jobs: list[dict]) -> list[dict]:
    """System holds everything reusable; user holds only what changes."""
    return [
        {"role": "system", "content": SYSTEM.format(profile=profile[:1400])},
        {"role": "user", "content": USER.format(jobs=_job_lines(jobs))},
    ]


def _job_lines(jobs: list[dict]) -> str:
    lines = []
    for i, j in enumerate(jobs):
        # Title, company and location carry most of the signal; the opening of
        # a description carries the rest.
        desc = " ".join((j.get("description") or "")[:240].split())
        lines.append(f"{i}|{j['company']}|{j['title']}|{j.get('location') or '?'}|{desc}")
    return "\n".join(lines)


def build_prompt(profile: str, jobs: list[dict]) -> str:
    """Single-string form, for backends that take one prompt (Cursor agents)."""
    msgs = build_messages(profile, jobs)
    return f"{msgs[0]['content']}\n\n{msgs[1]['content']}"


def clean_breakdown(raw) -> dict:
    """Accepts the positional list or a legacy dict."""
    if isinstance(raw, dict):
        src = [raw.get(k, 0) for k in KEYS]
    else:
        src = list(raw or [])
    out = {}
    for k, v in zip(KEYS, src + [0] * 6):
        try:
            out[k] = max(0, min(100, int(v)))
        except (TypeError, ValueError):
            out[k] = 0
    return out


def _salvage(blob: str) -> list:
    """Parse whatever individual [...] or {...} items survive a malformed array."""
    out = []
    for opener, closer in (("[", "]"), ("{", "}")):
        depth = 0
        start = -1
        for i, ch in enumerate(blob):
            if ch == opener:
                if depth == 0:
                    start = i
                depth += 1
            elif ch == closer and depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        item = json.loads(blob[start:i + 1])
                        if isinstance(item, (list, dict)) and item:
                            out.append(item)
                    except json.JSONDecodeError:
                        pass
                    start = -1
        if out:
            break
    return out


def _row(item, jobs: list[dict]) -> tuple | None:
    """Normalise one reply item into (job_id, score, reason, breakdown)."""
    if isinstance(item, dict):                      # tolerate the older shape
        ref, score = item.get("ref", -1), item.get("score", 0)
        reason, bd = item.get("reason", ""), item.get("breakdown")
    elif isinstance(item, list) and len(item) >= 2:
        ref, score = item[0], item[1]
        bd = item[2:8]
        reason = item[8] if len(item) > 8 else ""
    else:
        return None
    try:
        ref = int(ref)
    except (TypeError, ValueError):
        return None
    if not 0 <= ref < len(jobs):
        return None
    try:
        score = max(0, min(10, int(score)))
    except (TypeError, ValueError):
        score = 0
    return jobs[ref]["id"], score, str(reason)[:200], clean_breakdown(bd)


def parse(text: str, jobs: list[dict]) -> dict[str, tuple]:
    """Map a reply back onto job ids. Rows that are missing or malformed are
    dropped, never guessed - an unscored job is retried, an invented score is
    wrong forever."""
    open_at = text.find("[")
    if open_at == -1:
        raise ValueError(f"no JSON array in reply: {text[:160]}")
    close_at = text.rfind("]")
    blob = text[open_at:close_at + 1] if close_at > open_at else text[open_at:]
    try:
        items = json.loads(blob)
        if not isinstance(items, list):
            raise ValueError("reply was not an array")
        # A single flat row like [0,7,...] rather than a list of rows.
        if items and not isinstance(items[0], (list, dict)):
            items = [items]
    except (json.JSONDecodeError, ValueError):
        items = _salvage(blob)
        if not items:
            raise ValueError(f"unparseable reply: {text[:160]}")

    out: dict[str, tuple] = {}
    for item in items:
        row = _row(item, jobs)
        if row:
            out[row[0]] = (row[1], row[2], row[3])
    return out
