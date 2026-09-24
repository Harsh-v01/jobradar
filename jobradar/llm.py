"""Chat-completions client for providers that speak the OpenAI shape.

The active backend is Cursor (see cursor.py), which has no such endpoint. This
remains for Ollama and for the shared scorer interface."""
from __future__ import annotations

import json
import os
import re
import threading
import time

import requests

from . import batch

class RateLimited(Exception):
    """Provider throttled us. Distinct from a config error: the run should stop
    scoring and keep what it has, not abort and lose the scan."""


# Chat-completions providers. Cursor is NOT here: it has no such endpoint and
# lives in cursor.py behind the same scorer interface.
ENDPOINTS = {
    "xkiro":      ("https://api.xkiro.com/v1/chat/completions",       "XKIRO_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions",   "OPENROUTER_API_KEY"),
    "ollama":     ("http://localhost:11434/v1/chat/completions",      None),
}


class LLM:
    def __init__(self, provider: str, model: str):
        self.provider = provider
        if provider not in ENDPOINTS:
            raise ValueError(f"unknown provider '{provider}'")
        self.url, env = ENDPOINTS[provider]
        self.model = model
        self._lock = threading.Lock()   # one thread waits out a window, not all of them
        self.input_tokens = 0
        self.output_tokens = 0
        self.key = os.environ.get(env, "") if env else ""
        if env and (not self.key or self.key.startswith("paste-your")):
            raise RuntimeError(f"set {env} in your environment (free key, no card needed)")

    def _pace(self, headers) -> None:
        """Providers report a remaining token budget on every response. When it
        runs low, wait for the window to reset rather than push into a 429."""
        try:
            remaining = int(headers.get("x-ratelimit-remaining-tokens", "999999"))
        except ValueError:
            return
        if remaining > 1500:
            return
        reset = headers.get("x-ratelimit-reset-tokens", "")
        secs = 0.0
        m = re.match(r"(?:(\d+)m)?([\d.]+)s", reset)
        if m:
            secs = float(m.group(1) or 0) * 60 + float(m.group(2))
        with self._lock:
            time.sleep(min(max(secs, 1.0), 65))

    def chat(self, messages: list[dict], max_tokens: int = 1200, retries: int = 3) -> str:
        """Send prepared messages. Keeping the reusable half in a separate
        system message lets providers that cache prompt prefixes bill it at a
        fraction - xkiro counts cache reads at a tenth."""
        return self._post(messages, max_tokens, retries)

    def complete(self, prompt: str, max_tokens: int = 300, retries: int = 3) -> str:
        return self._post([{"role": "user", "content": prompt}], max_tokens, retries)

    def _post(self, messages: list[dict], max_tokens: int, retries: int) -> str:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        for attempt in range(retries):
            r = requests.post(self.url, headers=headers, json=body, timeout=60)
            if r.status_code == 429:           # free tiers rate-limit hard
                # Prefer the server's own Retry-After over guessing.
                wait = r.headers.get("retry-after")
                try:
                    delay = float(wait) if wait else 4 * (attempt + 1)
                except ValueError:
                    delay = 4 * (attempt + 1)
                time.sleep(min(delay, 30))
                continue
            if r.status_code >= 400:
                detail = r.text[:300]
                if r.status_code in (401, 403):
                    raise RuntimeError(f"{self.provider} rejected the API key: {detail}")
                if r.status_code == 404:
                    raise RuntimeError(
                        f"model '{self.model}' not found on {self.provider}. "
                        f"Check the current model list. ({detail})"
                    )
                raise RuntimeError(f"{self.provider} HTTP {r.status_code}: {detail}")
            self._pace(r.headers)
            payload = r.json()
            usage = payload.get("usage") or {}
            self.input_tokens += usage.get("prompt_tokens", 0) or 0
            self.output_tokens += usage.get("completion_tokens", 0) or 0
            text = payload["choices"][0]["message"]["content"]
            return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        raise RateLimited(f"{self.provider} rate-limited after {retries} retries")


SCORE_PROMPT = """You score how well a job fits a candidate.

CANDIDATE PROFILE:
{profile}

JOB:
Company: {company}
Title: {title}
Location: {location}
Description (truncated): {description}

Reply with ONLY a JSON object, no markdown fence:
{{"score": <0-10 integer>,
  "reason": "<max 20 words>",
  "breakdown": {{"skills": <0-100>, "seniority": <0-100>, "location": <0-100>,
                "domain": <0-100>, "recency": <0-100>, "reach": <0-100>}}}}

score  10 = near-perfect match, 0 = wrong field entirely.
breakdown, each 0-100, judged independently:
  skills    - how much of the required stack the candidate already uses
  seniority - how well the role's level matches their experience
  location  - whether they could actually work there (remote counts)
  domain    - familiarity with the industry or problem space
  recency   - how current their relevant experience is
  reach     - realistic chance of getting an interview
Penalize seniority mismatch and impossible locations."""


def score_job(llm: LLM, profile: str, job) -> tuple[int, str]:
    prompt = SCORE_PROMPT.format(
        profile=profile,
        company=job["company"], title=job["title"],
        location=job["location"] or "unspecified",
        description=(job["description"] if isinstance(job, dict) else "")[:700] or "n/a",
    )
    try:
        raw = llm.complete(prompt, max_tokens=150)
        match = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(match.group(0)) if match else {}
        raw_bd = data.get("breakdown") or {}
        breakdown = {}
        for k in ("skills", "seniority", "location", "domain", "recency", "reach"):
            try:
                breakdown[k] = max(0, min(100, int(raw_bd.get(k, 0))))
            except (TypeError, ValueError):
                breakdown[k] = 0
        return (int(data.get("score", 0)), str(data.get("reason", ""))[:200], breakdown)
    except RateLimited:
        raise            # caller stops scoring; unscored jobs wait for the next run
    except RuntimeError:
        raise            # key/model misconfiguration - fail loudly, do not score all zeros
    except Exception as e:  # noqa: BLE001 - one bad response must not abort the run
        return 0, f"scoring failed: {type(e).__name__}: {e}", {}


class ChatScorer:
    """Wraps a chat LLM in the same interface the other backends expose, so
    main.py and the web server never branch on which one is configured."""

    batch_size = 25

    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.llm = LLM(provider, model)
        self.input_tokens = 0

    def score_batch(self, profile: str, jobs: list[dict]) -> dict[str, tuple]:
        """One request per 25 jobs. Providers charge a fixed overhead per call
        (xkiro prepends ~550 tokens of its own), so per-job requests would pay
        that thousands of times over."""
        if not jobs:
            return {}
        # ~45 output tokens per job plus slack; capping this stops a model
        # rambling into a bill.
        cap = min(4000, 120 + 60 * len(jobs))
        text = self.llm.chat(batch.build_messages(profile, jobs), max_tokens=cap)
        return batch.parse(text, jobs)

    def score(self, profile: str, job) -> tuple:
        j = dict(job)
        j.setdefault("id", "single")
        return self.score_batch(profile, [j]).get(j["id"], (0, "no result", {}))

    def complete(self, prompt: str, **kw) -> str:
        return self.llm.complete(prompt, **kw)

    @property
    def tokens_used(self) -> int:
        return self.llm.input_tokens + self.llm.output_tokens


def make_scorer(provider: str, model: str, params: dict | None = None):
    """Returns None when scoring is disabled."""
    if provider in (None, "none", ""):
        return None
    if provider == "local":
        from .localmatch import LocalScorer
        return LocalScorer(model)          # `model` carries the resume text here
    if provider == "cursor":
        from .cursor import CursorScorer
        return CursorScorer(model, params)
    if provider == "jev":
        from .jev import JevScorer
        return JevScorer(model)
    return ChatScorer(provider, model)
