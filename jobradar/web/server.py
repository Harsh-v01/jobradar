"""Web UI for jobradar.

Serves a single page that shows every tracked job as a tile, tinted by how well
it matches the resume, and streams a live scan over Server-Sent Events so tiles
light up as they are scored.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import sources
from ..cursor import CursorError
from ..llm import RateLimited
from ..main import ROOT, get_profile, get_scorer, load_config, passes_filters
from ..resume import build_details, extract_text, parse_details
from ..store import Store

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Job Radar")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

_state = {"scanning": False, "started": 0.0}
_lock = threading.Lock()


def cfg():
    return load_config(ROOT / "config.yaml")


def store():
    return Store(ROOT / "jobradar.db")


@app.get("/")
def index():
    return FileResponse(str(STATIC / "index.html"))


@app.get("/api/profile")
def profile():
    c = cfg()
    cache = ROOT / ".resume_details.json"
    if cache.exists():
        cached = json.loads(cache.read_text())
        if cached.get("name"):          # only a real parse is worth keeping
            return cached
    try:
        text = extract_text(ROOT / c["resume_path"])
    except Exception as e:  # noqa: BLE001 - no resume at all
        return {"name": "", "headline": "", "location": "", "experience": [],
                "error": f"{type(e).__name__}: {e}"[:200]}

    # Reading a name off a resume does not need a model. Parse locally first so
    # the panel is populated with scoring off, then let a model refine it.
    details = parse_details(text)
    try:
        scorer = get_scorer(c)
        if scorer is not None and hasattr(scorer, "complete"):
            better = build_details(text, scorer)
            if better.get("name"):
                details = better
    except Exception as e:  # noqa: BLE001 - keep the local parse, note why
        details["error"] = f"{type(e).__name__}: {e}"[:200]
    if details.get("name"):
        cache.write_text(json.dumps(details))
    return details


@app.get("/api/state")
def state():
    c, s = cfg(), store()
    rows = s.db.execute(
        """SELECT id, company, title, url, location, score, reason, domain, source,
                  breakdown, first_seen, closed_at
           FROM jobs
           WHERE closed_at IS NULL          -- an expired posting is not a job
           ORDER BY score DESC NULLS LAST, company, title"""
    ).fetchall()
    jobs = _mosaic([dict(r) for r in rows], limit=4000)
    scored = [j for j in jobs if j["score"] is not None]
    threshold = c.get("llm", {}).get("min_score", 6)
    return {
        "jobs": jobs,
        "companies": [e["name"] for e in c.get("companies", [])],
        "stats": {
            "checked": len(scored),
            "total": len(jobs),
            "closed": s.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE closed_at IS NOT NULL").fetchone()[0],
            "would_hire": len([j for j in scored if j["score"] >= threshold]),
            "avg": round(sum(j["score"] for j in scored) / len(scored) / 10, 2) if scored else 0,
            "threshold": threshold,
        },
        "scanning": _state["scanning"],
        "scoring": c.get("llm", {}).get("provider", "none") not in ("none", None, ""),
    }


def _mosaic(jobs: list[dict], limit: int) -> list[dict]:
    """Round-robin the jobs across companies.

    Straight SQL order groups every Stripe role together, which reads as a block
    of identical logos. Dealing one job per company at a time gives the grid its
    mosaic look and puts every company on screen early. Scored jobs are dealt
    first so results stay visible near the top.
    """
    from collections import OrderedDict, deque

    buckets: OrderedDict[str, deque] = OrderedDict()
    for j in sorted(jobs, key=lambda x: (x["score"] is None, -(x["score"] or 0))):
        buckets.setdefault(j["company"], deque()).append(j)

    out: list[dict] = []
    while buckets and len(out) < limit:
        for company in list(buckets):
            if not buckets[company]:
                del buckets[company]
                continue
            out.append(buckets[company].popleft())
            if len(out) >= limit:
                break
    return out


def _scan_events(limit: int):
    """Generator of SSE events. Fetch first, then score, emitting per job."""
    c = cfg()
    started = time.time()
    q: queue.Queue = queue.Queue()

    def emit(kind, **data):
        q.put(f"data: {json.dumps({'type': kind, **data})}\n\n")

    def work():
        # sqlite3 connections are bound to the thread that created them, so the
        # worker opens its own rather than borrowing the request thread's.
        s = store()
        try:
            all_jobs, healthy = [], []
            for entry in c.get("companies", []):
                jobs, err = sources.fetch_company(entry)
                if not err:
                    healthy.append(entry["name"])
                    all_jobs.extend(jobs)
                emit("fetch", company=entry["name"], count=len(jobs), error=err or "")
            disc = c.get("discovery", {})
            if disc.get("enabled"):
                jobs, _ = sources.discover(disc.get("queries", []), disc.get("max_per_query", 40))
                all_jobs.extend(jobs)
                emit("fetch", company="discovery", count=len(jobs), error="")

            kept = [j for j in all_jobs if passes_filters(j, c.get("filters", {}))]
            desc = {j["id"]: j["description"] for j in kept}
            new = s.upsert(kept)
            closed = s.mark_closed(healthy)
            emit("fetched", new=len(new), closed=len(closed), total=len(kept))

            if c.get("llm", {}).get("provider", "none") == "none":
                emit("done", scored=0, elapsed=round(time.time() - started, 1))
                return

            scorer = get_scorer(c)
            prof = get_profile(c, scorer)
            rows = s.unscored(limit)
            emit("scoring", count=len(rows), backlog=s.unscored_count())

            # A batched backend (Cursor) judges many postings per agent run, so
            # chunk; a per-job backend gets chunks of one and behaves as before.
            batched = hasattr(scorer, "score_batch")
            size = int(c.get("llm", {}).get("batch_size", getattr(scorer, "batch_size", 25)))
            chunks = ([rows[i:i + size] for i in range(0, len(rows), size)]
                      if batched else [[r] for r in rows])

            done = 0
            for chunk in chunks:
                payload = []
                for row in chunk:
                    job = dict(row)
                    job["description"] = desc.get(row["id"], "")
                    payload.append(job)
                emit("looking", id=chunk[0]["id"], company=chunk[0]["company"],
                     title=(f"scoring {len(chunk)} jobs" if len(chunk) > 1
                            else chunk[0]["title"]),
                     domain=chunk[0]["domain"] or "")
                try:
                    if batched:
                        results = scorer.score_batch(prof, payload)
                    else:
                        results = {chunk[0]["id"]: scorer.score(prof, payload[0])}
                except (RateLimited, CursorError) as e:
                    emit("throttled", message=str(e), scored=done)
                    break
                for row in chunk:
                    score, reason, breakdown = results.get(row["id"], (0, "no result", {}))
                    s.save_score(row["id"], score, reason, breakdown)
                    done += 1
                    emit("scored", id=row["id"], score=score, reason=reason,
                         breakdown=breakdown, company=row["company"], title=row["title"],
                         domain=row["domain"] or "", url=row["url"] or "")

            emit("done", scored=done, elapsed=round(time.time() - started, 1))
        except Exception as e:  # noqa: BLE001 - always close the stream cleanly
            emit("error", message=f"{type(e).__name__}: {e}"[:300])
        finally:
            with _lock:
                _state["scanning"] = False
            q.put(None)

    with _lock:
        if _state["scanning"]:
            yield f"data: {json.dumps({'type': 'error', 'message': 'a scan is already running'})}\n\n"
            return
        _state["scanning"] = True
        _state["started"] = started

    threading.Thread(target=work, daemon=True).start()
    while True:
        item = q.get()
        if item is None:
            break
        yield item


@app.get("/api/search")
def search(q: str = "", limit: int = 4000):
    """Full-text search across title, company, location and description.

    Descriptions are far too large to ship to the browser (thousands of jobs
    x several KB), so matching happens in SQLite and only the ids come back.
    Every word must match somewhere, so extra words narrow the result.
    """
    words = [w for w in q.lower().split() if w][:6]
    if not words:
        return {"ids": None}          # null means "no query", not "no matches"
    s = store()
    clauses = " AND ".join(
        "(LOWER(title) LIKE ? OR LOWER(company) LIKE ? OR LOWER(location) LIKE ?"
        " OR LOWER(COALESCE(description,'')) LIKE ?)" for _ in words)
    params: list[str] = []
    for w in words:
        params.extend([f"%{w}%"] * 4)
    rows = s.db.execute(
        f"SELECT id FROM jobs WHERE closed_at IS NULL AND {clauses} LIMIT ?",
        (*params, limit),
    ).fetchall()
    return {"ids": [r["id"] for r in rows]}


@app.get("/api/scan")
def scan(limit: int = 120):
    return StreamingResponse(
        _scan_events(limit),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
