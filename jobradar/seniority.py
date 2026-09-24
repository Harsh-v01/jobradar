"""Seniority gate.

The scoring prompt asks the model to cap senior roles, but a prompt is a
request, not a guarantee - models comply inconsistently, and one that ignores
it buries every job the candidate could actually get. So the rule is also
enforced in code, where it is deterministic and free.

This is a title rule, not a judgement, which is why it can be applied to
scores that already exist without spending a token.
"""
from __future__ import annotations

import re

# Word-boundary matched so "Leadership Development Intern" is not caught by
# "lead", and "Senior" is, wherever it sits in the title.
SENIOR = (r"senior", r"sr\.?", r"staff", r"principal", r"lead", r"leads",
          r"head of", r"director", r"vp", r"vice president", r"chief",
          r"architect", r"manager", r"distinguished", r"fellow", r"iii", r"iv")

JUNIOR = (r"intern", r"internship", r"junior", r"jr\.?", r"new ?grad",
          r"graduate", r"entry[- ]level", r"associate", r"apprentice",
          r"trainee", r"early career", r"university", r"campus", r"fresher")

_SENIOR_RE = re.compile(r"\b(?:" + "|".join(SENIOR) + r")\b", re.I)
_JUNIOR_RE = re.compile(r"\b(?:" + "|".join(JUNIOR) + r")\b", re.I)

CAP = 3


def is_senior(title: str) -> bool:
    """A junior marker wins: 'Senior Engineer Intern' is an internship, and
    'Associate Director' is a director."""
    t = title or ""
    if _JUNIOR_RE.search(t) and not re.search(r"\bdirector\b|\bvp\b|\bhead of\b", t, re.I):
        return False
    return bool(_SENIOR_RE.search(t))


def apply_cap(score: int, title: str, reason: str = "") -> tuple[int, str]:
    """Returns the capped score and a reason that says why, so a low score is
    never mistaken for a poor skills match."""
    if score <= CAP or not is_senior(title):
        return score, reason
    note = f"capped at {CAP}: senior title"
    return CAP, f"{reason}; {note}" if reason else note
