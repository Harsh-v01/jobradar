"""SQLite state for JobRadar.

The database remembers jobs between runs so JobRadar can tell
what is NEW, what was already seen, and what has closed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    company             TEXT NOT NULL,
    title               TEXT NOT NULL,
    url                 TEXT,
    location            TEXT,
    source              TEXT,
    posted_at           REAL,
    first_seen          REAL NOT NULL,
    last_seen           REAL NOT NULL,
    closed_at           REAL,
    score               INTEGER,
    reason              TEXT,
    notified            INTEGER NOT NULL DEFAULT 0,
    domain              TEXT,
    breakdown           TEXT,
    alerted             INTEGER NOT NULL DEFAULT 0,
    description         TEXT,

    -- Job classification
    job_type            TEXT,
    salary              TEXT,
    tags                TEXT,

    -- India/fresher eligibility
    india_eligibility   TEXT,
    experience_level    TEXT
);

CREATE INDEX IF NOT EXISTS idx_first_seen
ON jobs(first_seen);

CREATE INDEX IF NOT EXISTS idx_job_type
ON jobs(job_type);

CREATE INDEX IF NOT EXISTS idx_india_eligibility
ON jobs(india_eligibility);

CREATE TABLE IF NOT EXISTS runs (
    ts INTEGER PRIMARY KEY,
    new_count INTEGER,
    closed_count INTEGER,
    errors TEXT
);
"""


class Store:
    def __init__(
        self,
        path: str | Path = "jobradar.db",
    ):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row

        self.db.executescript(SCHEMA)

        # Make older JobRadar databases compatible with the
        # newer schema without deleting existing jobs.
        self._migrate()

        self.db.commit()

        self.last_upsert_ts = time.time()

    # ------------------------------------------------------------------
    # Database migration
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        """Add columns introduced after the database was first created."""

        have = {
            row["name"]
            for row in self.db.execute(
                "PRAGMA table_info(jobs)"
            )
        }

        migrations = {
            "domain": "TEXT",
            "breakdown": "TEXT",
            "alerted": "INTEGER NOT NULL DEFAULT 0",
            "description": "TEXT",
            "job_type": "TEXT",
            "salary": "TEXT",
            "tags": "TEXT",
            "india_eligibility": "TEXT",
            "experience_level": "TEXT",
        }

        for column, ddl in migrations.items():
            if column not in have:
                self.db.execute(
                    f"ALTER TABLE jobs ADD COLUMN "
                    f"{column} {ddl}"
                )

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def upsert(
        self,
        jobs: list[dict],
    ) -> list[dict]:
        """Insert jobs and return only jobs never seen before."""

        now = time.time()

        self.last_upsert_ts = now

        fresh = []

        for job in jobs:
            existing = self.db.execute(
                "SELECT id FROM jobs WHERE id = ?",
                (job["id"],),
            ).fetchone()

            if existing:
                # The job is still present.
                # Refresh last_seen and reopen it if necessary.
                self.db.execute(
                    """
                    UPDATE jobs
                    SET
                        last_seen = ?,
                        closed_at = NULL,
                        company = ?,
                        title = ?,
                        url = ?,
                        location = ?,
                        source = ?,
                        posted_at = ?,
                        domain = ?,
                        description = ?,
                        job_type = ?,
                        salary = ?,
                        tags = ?,
                        india_eligibility = ?,
                        experience_level = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        job.get("company", ""),
                        job.get("title", ""),
                        job.get("url", ""),
                        job.get("location", ""),
                        job.get("source", ""),
                        job.get("posted_at"),
                        job.get("domain", ""),
                        (job.get("description") or "")[:1500],
                        job.get("job_type", "unknown"),
                        job.get("salary", ""),
                        self._tags_to_text(
                            job.get("tags")
                        ),
                        job.get(
                            "india_eligibility",
                            "unknown",
                        ),
                        job.get(
                            "experience_level",
                            "unknown",
                        ),
                        job["id"],
                    ),
                )

            else:
                self.db.execute(
                    """
                    INSERT INTO jobs (
                        id,
                        company,
                        title,
                        url,
                        location,
                        source,
                        posted_at,
                        first_seen,
                        last_seen,
                        domain,
                        description,
                        job_type,
                        salary,
                        tags,
                        india_eligibility,
                        experience_level
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        job["id"],
                        job.get("company", ""),
                        job.get("title", ""),
                        job.get("url", ""),
                        job.get("location", ""),
                        job.get("source", ""),
                        job.get("posted_at"),
                        now,
                        now,
                        job.get("domain", ""),
                        (job.get("description") or "")[:1500],
                        job.get("job_type", "unknown"),
                        job.get("salary", ""),
                        self._tags_to_text(
                            job.get("tags")
                        ),
                        job.get(
                            "india_eligibility",
                            "unknown",
                        ),
                        job.get(
                            "experience_level",
                            "unknown",
                        ),
                    ),
                )

                fresh.append(job)

        self.db.commit()

        return fresh

    @staticmethod
    def _tags_to_text(tags) -> str:
        """Store tags safely as JSON text."""

        if not tags:
            return ""

        if isinstance(tags, str):
            return tags

        try:
            return json.dumps(tags)
        except (TypeError, ValueError):
            return str(tags)

    # ------------------------------------------------------------------
    # Closing jobs
    # ------------------------------------------------------------------

    def mark_closed(
        self,
        companies: list[str],
        run_ts: float | None = None,
    ) -> list[sqlite3.Row]:
        """Mark jobs as closed only for successfully fetched companies."""

        run_ts = (
            run_ts
            if run_ts is not None
            else getattr(
                self,
                "last_upsert_ts",
                time.time(),
            )
        )

        if not companies:
            return []

        marks = ",".join(
            "?" * len(companies)
        )

        rows = self.db.execute(
            f"""
            SELECT *
            FROM jobs
            WHERE company IN ({marks})
              AND closed_at IS NULL
              AND last_seen < ?
            """,
            (
                *companies,
                run_ts,
            ),
        ).fetchall()

        for row in rows:
            self.db.execute(
                """
                UPDATE jobs
                SET closed_at = ?
                WHERE id = ?
                """,
                (
                    run_ts,
                    row["id"],
                ),
            )

        self.db.commit()

        return rows

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def save_score(
        self,
        job_id: str,
        score: int,
        reason: str,
        breakdown: dict | None = None,
    ) -> None:

        self.db.execute(
            """
            UPDATE jobs
            SET
                score = ?,
                reason = ?,
                breakdown = ?
            WHERE id = ?
            """,
            (
                score,
                reason,
                json.dumps(
                    breakdown or {}
                ),
                job_id,
            ),
        )

        self.db.commit()

    def unscored(
        self,
        limit: int,
    ) -> list[sqlite3.Row]:

        return self.db.execute(
            """
            SELECT *
            FROM jobs
            WHERE score IS NULL
              AND closed_at IS NULL
            ORDER BY first_seen DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def unscored_count(self) -> int:
        return self.db.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE score IS NULL
              AND closed_at IS NULL
            """
        ).fetchone()[0]

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def since(
        self,
        seconds: float,
        min_score: int = 0,
    ) -> list[sqlite3.Row]:

        cutoff = time.time() - seconds

        return self.db.execute(
            """
            SELECT *
            FROM jobs
            WHERE first_seen >= ?
              AND closed_at IS NULL
              AND (
                  score IS NULL
                  OR score >= ?
              )
            ORDER BY
                score DESC NULLS LAST,
                first_seen DESC
            """,
            (
                cutoff,
                min_score,
            ),
        ).fetchall()

    def closed_since(
        self,
        seconds: float,
    ) -> list[sqlite3.Row]:

        cutoff = time.time() - seconds

        return self.db.execute(
            """
            SELECT *
            FROM jobs
            WHERE closed_at >= ?
            ORDER BY closed_at DESC
            """,
            (cutoff,),
        ).fetchall()

    def unnotified(
        self,
        min_score: int,
        require_scored: bool = False,
    ) -> list[sqlite3.Row]:

        """Return jobs waiting for the digest."""

        scored_filter = (
            "AND score IS NOT NULL"
            if require_scored
            else ""
        )

        return self.db.execute(
            f"""
            SELECT *
            FROM jobs
            WHERE notified = 0
              AND closed_at IS NULL
              {scored_filter}
              AND (
                  score IS NULL
                  OR score >= ?
              )
            ORDER BY
                score DESC NULLS LAST,
                first_seen DESC
            """,
            (min_score,),
        ).fetchall()

    # ------------------------------------------------------------------
    # Instant alerts
    # ------------------------------------------------------------------

    def unalerted(self) -> list[sqlite3.Row]:
        """Jobs that have never been sent through instant alerts."""

        return self.db.execute(
            """
            SELECT *
            FROM jobs
            WHERE alerted = 0
              AND closed_at IS NULL
            ORDER BY first_seen DESC
            """
        ).fetchall()

    def baselined_companies(self) -> set[str]:
        """Companies that have already gone through an alert baseline."""

        return {
            row["company"]
            for row in self.db.execute(
                """
                SELECT DISTINCT company
                FROM jobs
                WHERE alerted = 1
                """
            )
        }

    def mark_alerted(
        self,
        ids: list[str],
    ) -> None:

        self.db.executemany(
            """
            UPDATE jobs
            SET alerted = 1
            WHERE id = ?
            """,
            [
                (job_id,)
                for job_id in ids
            ],
        )

        self.db.commit()

    def mark_notified(
        self,
        ids: list[str],
    ) -> None:

        self.db.executemany(
            """
            UPDATE jobs
            SET notified = 1
            WHERE id = ?
            """,
            [
                (job_id,)
                for job_id in ids
            ],
        )

        self.db.commit()

    # ------------------------------------------------------------------
    # Run history
    # ------------------------------------------------------------------

    def log_run(
        self,
        new_count: int,
        closed_count: int,
        errors: list[str],
    ) -> None:

        self.db.execute(
            """
            INSERT OR REPLACE INTO runs
            VALUES (?, ?, ?, ?)
            """,
            (
                int(time.time()),
                new_count,
                closed_count,
                "\n".join(errors),
            ),
        )

        self.db.commit()

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def stats(self) -> dict:

        row = self.db.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(
                    closed_at IS NULL
                ) AS open
            FROM jobs
            """
        ).fetchone()

        return {
            "total": row["total"] or 0,
            "open": row["open"] or 0,
        }
