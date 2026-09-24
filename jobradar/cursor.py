"""Cursor Cloud Agents backend.

Cursor exposes no chat-completions endpoint. The only way to get text out of it
is to create an agent with a prompt and poll its run for the final reply, which
is a heavy round trip: seconds per call, and 20 requests/minute.

So this backend scores in BATCHES. One agent run judges many postings and
returns a JSON array, turning ~2300 single calls into ~50 batched ones. That is
the difference between unusable and a few minutes.
"""
from __future__ import annotations

import json
import os
import time

import requests

from . import batch

BASE = "https://api.cursor.com/v1"
KEYS = ("skills", "seniority", "location", "domain", "recency", "reach")




class CursorError(RuntimeError):
    """Configuration or API problem that should stop the run loudly."""


class CursorScorer:
    provider = "cursor"

    def __init__(self, model: str = "", params: dict | None = None):
        self.key = (os.environ.get("CURSOR_API_KEY") or "").strip()
        if not self.key or self.key.startswith("paste-your"):
            raise CursorError(
                "CURSOR_API_KEY is not set (still the placeholder in run.sh?). "
                "Create one at cursor.com/dashboard -> Integrations -> API Keys."
            )
        # Scoring is classification, not deliberation: reasoning tokens are
        # pure cost here, so the default turns them down as far as the model
        # allows. Override both in config.yaml.
        self.model = model or "gemini-3.8-flash"
        self.params = params if params is not None else {"reasoning_effort": "low"}
        self.batch_size = 25
        self.poll_seconds = 4
        self.timeout_seconds = 420

    def _model_selection(self) -> dict:
        sel: dict = {"id": self.model}
        if self.params:
            sel["params"] = [{"id": k, "value": v} for k, v in self.params.items()]
        return sel

    # -- HTTP ---------------------------------------------------------------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

    def _run_agent(self, prompt: str) -> str:
        """Create an agent, wait for its run, return the final reply text."""
        r = requests.post(
            f"{BASE}/agents", headers=self._headers(), timeout=60,
            json={"prompt": {"text": prompt}, "model": self._model_selection()},
        )
        if r.status_code == 401:
            raise CursorError("Cursor rejected the API key.")
        if r.status_code >= 400:
            detail = r.text[:300]
            # These two are account settings, not bugs - say exactly what to flip.
            if "Storage mode" in detail:
                raise CursorError(
                    "Cursor: storage mode is disabled. Turn on storage in Cursor "
                    "Settings so agents can run via the API."
                )
            if "Privacy Mode" in detail:
                raise CursorError(
                    "Cursor: your account is on Privacy Mode (Legacy), which blocks "
                    "cloud agents. Switch to the current Privacy Mode in Cursor settings."
                )
            raise CursorError(f"Cursor HTTP {r.status_code}: {detail}")

        agent = r.json()
        agent_id = agent.get("id")
        run_id = agent.get("latestRunId") or (agent.get("latestRun") or {}).get("id")
        if not agent_id or not run_id:
            raise CursorError(f"Cursor returned no run to poll: {str(agent)[:200]}")

        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            time.sleep(self.poll_seconds)
            rr = requests.get(f"{BASE}/agents/{agent_id}/runs/{run_id}",
                              headers=self._headers(), timeout=60)
            if rr.status_code >= 400:
                raise CursorError(f"Cursor poll HTTP {rr.status_code}: {rr.text[:200]}")
            run = rr.json()
            status = (run.get("status") or "").upper()
            if status in ("FINISHED", "COMPLETED", "SUCCEEDED"):
                return run.get("result") or ""
            if status in ("FAILED", "CANCELLED", "ERROR", "EXPIRED"):
                raise CursorError(f"Cursor run {status}: {str(run.get('error'))[:200]}")
        raise CursorError(f"Cursor run did not finish within {self.timeout_seconds}s")

    # -- scoring ------------------------------------------------------------
    def score_batch(self, profile: str, jobs: list[dict]) -> dict[str, tuple]:
        """Returns {job_id: (score, reason, breakdown)} for the jobs it could score."""
        if not jobs:
            return {}
        text = self._run_agent(batch.build_prompt(profile, jobs))
        try:
            return batch.parse(text, jobs)
        except (ValueError, json.JSONDecodeError) as e:
            raise CursorError(f"Cursor returned unusable output: {e}") from e

    def score(self, profile: str, job) -> tuple:
        """Single-job path, for callers that have not been batched."""
        j = dict(job)
        j.setdefault("id", "single")
        got = self.score_batch(profile, [j])
        return got.get(j["id"], (0, "no result", {}))

    def complete(self, prompt: str, max_tokens: int = 300) -> str:
        """Plain text out of an agent run.

        Used for the one-off resume summary and profile extraction, not for
        scoring - scoring goes through score_batch. max_tokens is accepted for
        interface compatibility; Cursor has no such parameter.
        """
        return self._run_agent(
            prompt + "\n\nAnswer directly. Do not browse, run commands, or create files."
        )
