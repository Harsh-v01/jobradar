"""Jev backend (TypeSafe AI System One model).

Jev is a much better shape for this job than a chat LLM. Scoring a posting is
not a writing task - it is "score this against a scale" and "pick one of these
options", which are exactly Jev's Score and Choice primitives. One parallel
query returns every field as a typed value with calibrated probabilities, so
there is no JSON to coax out of prose and no parse step that can fail.

It also needs no resume *summary*: Jev takes unstructured state directly, so we
hand it the resume text and the posting together and ask typed questions.
"""
from __future__ import annotations

import os

# 0..4 -> rescaled to the 0..10 the rest of the app speaks.
FIT_LEVELS = [
    "wrong field entirely - the candidate has none of the required background",
    "adjacent field - some transferable skills but a clear stretch",
    "plausible - meets roughly half the stated requirements",
    "strong - meets most requirements with relevant direct experience",
    "excellent - meets or exceeds every requirement, an obvious match",
]

SENIORITY = {
    "under": "the role is more senior than the candidate's experience supports",
    "match": "the role's seniority matches the candidate's experience",
    "over": "the role is junior to what the candidate has already done",
}

LOCATION = {
    "ok": "the candidate could work here - remote, or a location they can reach",
    "unclear": "the posting does not state a location clearly enough to judge",
    "blocked": "onsite in a place the candidate cannot plausibly work from",
}


class JevScorer:
    """Matches the interface main.py expects: .score(profile, job) -> (int, str)."""

    provider = "jev"

    def __init__(self, model: str | None = None):
        try:
            from typesafe_sdk import Choice, Score, TypeSafeClient
        except ImportError as e:
            raise RuntimeError("pip install typesafe-sdk to use the Jev backend") from e
        key = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
        if not key or key.startswith("paste-your"):
            raise RuntimeError(
                "TYPESAFE_API_KEY is not set (still the placeholder in run.sh?). "
                "Get one at console.typesafe.ai, or set llm.provider to 'cursor' "
                "or 'none' in config.yaml."
            )

        self._Score, self._Choice = Score, Choice
        self.client = TypeSafeClient(**({"model": model} if model else {}))
        self.input_tokens = 0   # tracked so a run can report what it cost

    def _questions(self) -> dict:
        return {
            "fit": self._Score(
                instructions="How well does this candidate fit this specific job?",
                criteria=FIT_LEVELS,
            ),
            "seniority": self._Choice(
                instructions="How does the role's seniority compare to the candidate's?",
                criteria={k: v for k, v in SENIORITY.items()},
            ),
            "location": self._Choice(
                instructions="Can this candidate actually work in this role's location?",
                criteria={k: v for k, v in LOCATION.items()},
            ),
        }

    def score(self, profile: str, job) -> tuple[int, str]:
        state = (
            f"CANDIDATE RESUME:\n{profile[:8000]}\n\n"
            f"JOB POSTING:\n"
            f"Company: {job['company']}\n"
            f"Title: {job['title']}\n"
            f"Location: {job['location'] or 'unspecified'}\n"
            f"Description: {(job.get('description') or 'n/a')[:4000]}"
        )
        try:
            result = self.client.system_one(state, self._questions())
        except Exception as e:  # noqa: BLE001 - one bad call must not abort the run
            return 0, f"scoring failed: {type(e).__name__}", {}

        usage = getattr(result, "usage", None)
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0

        fit = result.scores["fit"]
        seniority = result.choices["seniority"].choice
        location = result.choices["location"].choice

        # fit.score is a probability-weighted mean in 0..len-1; rescale to 0..10.
        score = fit.score / (len(FIT_LEVELS) - 1) * 10

        # Jev hands back calibrated penalties instead of a prose caveat.
        if location == "blocked":
            score *= 0.3
        if seniority in ("under", "over"):
            score *= 0.75

        confidence = getattr(fit, "confidence", None)
        bits = [f"seniority {seniority}", f"location {location}"]
        if confidence is not None:
            bits.append(f"confidence {confidence:.0%}")
        breakdown = {
            "skills": round(fit.score / (len(FIT_LEVELS) - 1) * 100),
            "seniority": {"match": 100, "under": 35, "over": 55}.get(seniority, 50),
            "location": {"ok": 100, "unclear": 55, "blocked": 10}.get(location, 50),
            "domain": round(fit.score / (len(FIT_LEVELS) - 1) * 100),
            "recency": 100,
            "reach": round(score * 10),
        }
        return round(score), ", ".join(bits), breakdown

    # $0.042 per million input tokens; output is free.
    INPUT_COST_PER_MTOK = 0.042

    def cost_usd(self) -> float:
        return self.input_tokens / 1_000_000 * self.INPUT_COST_PER_MTOK
