"""Relevance gate.

The seniority gate stops a senior role riding a strong skills match. This is
the mirror problem: a role in the wrong field riding a strong *seniority*
match. "Intern, Investor Center" scored 8/10 for a full-stack developer purely
because it is an internship and the candidate is a new graduate.

A job the candidate has neither the skills nor the domain background for is not
a near miss, whatever else lines up. Both sub-scores have to be weak before the
cap applies, so a role that is unfamiliar in one dimension but strong in the
other still gets through.

Like the seniority rule this is arithmetic on sub-scores the model already
returned, so it costs nothing and can be applied to existing rows.
"""
from __future__ import annotations

SKILLS_FLOOR = 60
DOMAIN_FLOOR = 60
CAP = 3


def is_irrelevant(breakdown: dict | None) -> bool:
    if not breakdown:
        return False
    try:
        skills = int(breakdown.get("skills", 0))
        domain = int(breakdown.get("domain", 0))
    except (TypeError, ValueError):
        return False
    # A breakdown of all zeros means the model returned nothing usable, not
    # that the job is a bad fit - do not cap on missing data.
    if skills == 0 and domain == 0 and not any(breakdown.values()):
        return False
    return skills < SKILLS_FLOOR and domain < DOMAIN_FLOOR


def apply_cap(score: int, breakdown: dict | None, reason: str = "") -> tuple[int, str]:
    if score <= CAP or not is_irrelevant(breakdown):
        return score, reason
    note = f"capped at {CAP}: wrong field"
    return CAP, f"{reason}; {note}" if reason else note
