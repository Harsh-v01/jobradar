"""SQLite state. The whole point: remember what we have already seen, so a
later run can tell 'this is NEW' from 'this was here last week'."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT,
    location    TEXT,
    source      TEXT,
    posted_at   REAL,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    closed_at   REAL,
    score       INTEGER,
    reason      TEXT,
    notified    INTEGER NOT NULL DEFAULT 0,
    domain      TEXT,
    breakdown   TEXT,
    alerted     INTEGER NOT NULL DEFAULT 0,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_first_seen ON jobs(first_seen);
CREATE TABLE IF NOT EXISTS runs (
    ts INTEGER PRIMARY KEY, new_count INTEGER, closed_count INTEGER, errors TEXT
);
"""


class Store:
    def __init__(self, path: str | Path = "jobradar.db"):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()
        self.last_upsert_ts = time.time()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for col, ddl in (("domain", "TEXT"), ("breakdown", "TEXT"),
                         ("alerted", "INTEGER NOT NULL DEFAULT 0"),
                         ("description", "TEXT")):
            if col not in have:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")

    # -- ingest -------------------------------------------------------------
    def upsert(self, jobs: list[dict]) -> list[dict]:
        """Insert jobs, returning only the ones never seen before."""
        now = time.time()
        self.last_upsert_ts = now
        fresh = []
        for j in jobs:
            row = self.db.execute("SELECT id FROM jobs WHERE id = ?", (j["id"],)).fetchone()
            if row:
                self.db.execute(
                    "UPDATE jobs SET last_seen = ?, closed_at = NULL WHERE id = ?", (now, j["id"])
                )
            else:
                self.db.execute(
                    """INSERT INTO jobs (id, company, title, url, location, source,
                                         posted_at, first_seen, last_seen, domain, description)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (j["id"], j["company"], j["title"], j["url"], j["location"],
                     j["source"], j["posted_at"], now, now, j.get("domain", ""),
                     # Trimmed: enough for search to work, not so much that the
                     # database balloons with boilerplate benefits sections.
                     (j.get("description") or "")[:1500]),
                )
                fresh.append(j)
        self.db.commit()
        return fresh

    def mark_closed(self, companies: list[str], run_ts: float | None = None) -> list[sqlite3.Row]:
        """A tracked job that stopped appearing on its board is treated as closed.
        Only applies to companies we actually fetched successfully this run.

        run_ts defaults to the timestamp of the last upsert, so 'was not seen in
        the run that just finished' cannot be skewed by the caller's clock."""
        run_ts = run_ts if run_ts is not None else getattr(self, "last_upsert_ts", time.time())
        if not companies:
            return []
        marks = ",".join("?" * len(companies))
        rows = self.db.execute(
            f"""SELECT * FROM jobs WHERE company IN ({marks})
                AND closed_at IS NULL AND last_seen < ?""",
            (*companies, run_ts),
        ).fetchall()
        for r in rows:
            self.db.execute("UPDATE jobs SET closed_at = ? WHERE id = ?", (run_ts, r["id"]))
        self.db.commit()
        return rows

    # -- scoring ------------------------------------------------------------
    def save_score(self, job_id: str, score: int, reason: str,
                   breakdown: dict | None = None) -> None:
        self.db.execute(
            "UPDATE jobs SET score = ?, reason = ?, breakdown = ? WHERE id = ?",
            (score, reason, json.dumps(breakdown or {}), job_id),
        )
        self.db.commit()

    def unscored(self, limit: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM jobs WHERE score IS NULL AND closed_at IS NULL "
            "ORDER BY first_seen DESC LIMIT ?", (limit,)
        ).fetchall()

    # -- reporting ----------------------------------------------------------
    def unscored_count(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE score IS NULL AND closed_at IS NULL"
        ).fetchone()[0]

    def since(self, seconds: float, min_score: int = 0) -> list[sqlite3.Row]:
        cutoff = time.time() - seconds
        return self.db.execute(
            """SELECT * FROM jobs WHERE first_seen >= ? AND closed_at IS NULL
               AND (score IS NULL OR score >= ?)
               ORDER BY score DESC NULLS LAST, first_seen DESC""",
            (cutoff, min_score),
        ).fetchall()

    def closed_since(self, seconds: float) -> list[sqlite3.Row]:
        cutoff = time.time() - seconds
        return self.db.execute(
            "SELECT * FROM jobs WHERE closed_at >= ? ORDER BY closed_at DESC", (cutoff,)
        ).fetchall()

    def unnotified(self, min_score: int, require_scored: bool = False) -> list[sqlite3.Row]:
        """require_scored=True when a scorer is configured: a job the run had no
        budget left to score must wait for the next run rather than arrive
        unranked. Without it, a first scan of a big watchlist would email every
        job that overflowed max_to_score."""
        unscored = "AND score IS NOT NULL" if require_scored else ""
        return self.db.execute(
            f"""SELECT * FROM jobs WHERE notified = 0 AND closed_at IS NULL
                {unscored} AND (score IS NULL OR score >= ?)
                ORDER BY score DESC NULLS LAST, first_seen DESC""",
            (min_score,),
        ).fetchall()

    def unalerted(self) -> list[sqlite3.Row]:
        """Jobs never sent as an instant alert. Separate from `notified` so the
        weekly digest and the alerts do not consume each other's queue."""
        return self.db.execute(
            """SELECT * FROM jobs WHERE alerted = 0 AND closed_at IS NULL
               ORDER BY first_seen DESC"""
        ).fetchall()

    def baselined_companies(self) -> set[str]:
        """Companies that have been through at least one alert run. Anything
        else is being seen for the first time, so its whole board is history,
        not news."""
        return {r["company"] for r in
                self.db.execute("SELECT DISTINCT company FROM jobs WHERE alerted = 1")}

    def mark_alerted(self, ids: list[str]) -> None:
        self.db.executemany("UPDATE jobs SET alerted = 1 WHERE id = ?", [(i,) for i in ids])
        self.db.commit()

    def mark_notified(self, ids: list[str]) -> None:
        self.db.executemany("UPDATE jobs SET notified = 1 WHERE id = ?", [(i,) for i in ids])
        self.db.commit()

    def log_run(self, new_count: int, closed_count: int, errors: list[str]) -> None:
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
                        (int(time.time()), new_count, closed_count, "\n".join(errors)))
        self.db.commit()

    def stats(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) total, SUM(closed_at IS NULL) open FROM jobs"
        ).fetchone()
        return {"total": row["total"] or 0, "open": row["open"] or 0}
