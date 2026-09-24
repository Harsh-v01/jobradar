"""Keyword matching against the resume. No model, no network, no key.

This is deliberately modest: it compares the skills named in your resume with
the words in a posting, checks the title for seniority signals, and checks the
location. It is not judgement - it cannot tell that "Rails" implies web work, or
that a posting is a bad culture fit. What it can do is rank a board of 2000
jobs by how many of your actual skills appear in each one, instantly and free.

Scores from here are labelled 'keyword match' so they are never mistaken for
the model's reading.
"""
from __future__ import annotations

import re
import time

# Tokens worth recognizing even when a resume does not list them under SKILLS.
VOCAB = {
    "javascript", "typescript", "python", "java", "golang", "go", "rust", "ruby",
    "php", "swift", "kotlin", "scala", "c", "c++", "c#", "sql", "bash",
    "react", "angular", "vue", "svelte", "next.js", "nextjs", "node", "node.js",
    "express", "django", "flask", "fastapi", "rails", "spring", "laravel",
    "react native", "flutter", "android", "ios",
    "mongodb", "postgresql", "postgres", "mysql", "redis", "sqlite", "dynamodb",
    "elasticsearch", "kafka", "rabbitmq", "graphql", "rest", "grpc",
    "docker", "kubernetes", "aws", "gcp", "azure", "terraform", "jenkins",
    "ci/cd", "git", "github", "gitlab", "linux", "nginx",
    "tensorflow", "pytorch", "keras", "pandas", "numpy", "scikit-learn",
    "llm", "rag", "nlp", "machine learning", "deep learning", "computer vision",
    "jest", "pytest", "cypress", "playwright", "selenium",
    "html", "css", "tailwind", "sass", "webpack", "vite", "figma",
    "jwt", "oauth", "websocket", "socket.io", "microservices", "api",
}

_LABELS = {"languages", "frontend", "backend", "databases", "tools", "ai", "ml",
           "ai / ml", "frameworks", "other", "technologies", "concepts"}

JUNIOR = ("intern", "internship", "junior", "jr.", "entry level", "entry-level",
          "graduate", "new grad", "associate", "trainee", "apprentice", "fresher")
SENIOR = ("senior", "sr.", "staff", "principal", "lead", "head of", "director",
          "vp ", "vice president", "chief", "architect", "manager", "distinguished")
REMOTE = ("remote", "anywhere", "work from home", "distributed")


def extract_skills(resume_text: str) -> set[str]:
    """Skills named in the resume: the SKILLS section plus any known token."""
    text = resume_text.lower()
    found = {v for v in VOCAB if _mentions(text, v)}

    # A SKILLS section lists things the vocabulary may not know about.
    m = re.search(r"^\s*(?:technical\s+)?skills\s*:?\s*$(.*?)(?=^\s*[A-Z][A-Z ]{3,}\s*$|\Z)",
                  resume_text, re.I | re.M | re.S)
    if m:
        for chunk in re.split(r"[,;:|\n]", m.group(1)):
            tok = chunk.strip().lower()
            tok = re.sub(r"^(languages|frontend|backend|databases|tools|ai\s*/\s*ml)\s*", "", tok)
            # Drop fragments left by the section labels ("ai /", "tools", "-").
            if not re.search(r"[a-z]{2}", tok):
                continue
            tok = tok.strip(" /-&.")
            if 2 <= len(tok) <= 24 and tok not in _LABELS:
                found.add(tok)
    return {s for s in found if s}


def _mentions(haystack: str, needle: str) -> bool:
    """Word-boundary match that survives punctuation like 'Node.js' or 'C++'."""
    return re.search(rf"(?<![\w.+#]){re.escape(needle)}(?![\w.+#])", haystack) is not None


class LocalScorer:
    """Same interface as the model backends: .score() -> (score, reason, breakdown)."""

    provider = "local"

    def __init__(self, resume_text: str = ""):
        self.skills = extract_skills(resume_text)
        self.junior_profile = bool(re.search(r"\bintern\b|\bb\.?tech\b|\bstudent\b",
                                             resume_text, re.I))

    def score(self, profile: str, job) -> tuple[int, str, dict]:
        title = (job.get("title") or "").lower()
        blob = f"{title} {(job.get('description') or '').lower()}"
        where = (job.get("location") or "").lower()

        hits = sorted(s for s in self.skills if _mentions(blob, s))
        # Six overlapping skills is a strong signal; more adds little.
        skills = min(100, round(len(hits) / 6 * 100)) if self.skills else 0

        senior_role = any(k in title for k in SENIOR)
        junior_role = any(k in title for k in JUNIOR)
        if self.junior_profile:
            seniority = 20 if senior_role else (100 if junior_role else 65)
        else:
            seniority = 55 if junior_role else (75 if senior_role else 70)

        if any(k in where for k in REMOTE) or not where:
            location = 85
        elif "india" in where:
            location = 100
        else:
            location = 30

        recency = 60
        posted = job.get("posted_at")
        if posted:
            days = (time.time() - posted) / 86400
            recency = 100 if days <= 7 else 80 if days <= 30 else 50 if days <= 90 else 25

        domain = min(100, round(len(hits) / 4 * 100)) if hits else 0
        reach = round(skills * 0.5 + seniority * 0.35 + location * 0.15)

        # Skills dominate: a title match with no overlapping stack is noise.
        blended = skills * 0.45 + seniority * 0.25 + location * 0.15 + domain * 0.15
        score = max(0, min(10, round(blended / 10)))

        if hits:
            shown = ", ".join(hits[:4])
            reason = f"keyword match: {shown}" + (f" +{len(hits) - 4} more" if len(hits) > 4 else "")
        else:
            reason = "keyword match: no overlapping skills found"
        if senior_role and self.junior_profile:
            reason += "; senior title"
        if location == 30:
            reason += "; location may not work"

        return score, reason[:200], {
            "skills": skills, "seniority": seniority, "location": location,
            "domain": domain, "recency": recency, "reach": reach,
        }
