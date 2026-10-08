"""jobradar CLI.

scan     fetch every source, diff against the DB, score new jobs
digest   email everything not yet reported
run      scan + digest in one shot
list     print recent finds in the terminal
test     send a sample email to prove SMTP works
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from . import alerts as alerts_mod
from . import digest as digest_mod
from . import sources
from .budget import Budget
from .cursor import CursorError
from .llm import RateLimited, make_scorer
from .localmatch import LocalScorer
from .relevance import apply_cap as apply_relevance_cap
from .resume import build_profile, extract_text
from .seniority import apply_cap
from .store import Store


ROOT = Path(__file__).resolve().parent.parent
PROFILE_CACHE = ROOT / ".resume_profile.json"


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(
            f"No config at {path}. "
            "Copy config.example.yaml to config.yaml and edit it."
        )
    return yaml.safe_load(path.read_text())


def get_scorer(cfg: dict):
    spec = cfg.get("llm", {})
    provider = spec.get("provider", "none")

    if provider == "local":
        path = Path(cfg["resume_path"])
        resume_file = path if path.is_absolute() else ROOT / path
        return make_scorer("local", extract_text(resume_file))

    return make_scorer(
        provider,
        spec.get("model", ""),
        spec.get("model_params"),
    )


def get_profile(cfg: dict, scorer, force: bool = False) -> str:
    resume_path = (
        ROOT / cfg["resume_path"]
        if not Path(cfg["resume_path"]).is_absolute()
        else Path(cfg["resume_path"])
    )

    stamp = resume_path.stat().st_mtime if resume_path.exists() else 0

    summarizer = (
        scorer
        if scorer is not None and hasattr(scorer, "complete")
        else None
    )

    # Keep model-generated and raw profiles separate.
    kind = "model" if summarizer else "raw"

    if PROFILE_CACHE.exists() and not force:
        try:
            cached = json.loads(PROFILE_CACHE.read_text())
            if (
                cached.get("mtime") == stamp
                and cached.get("kind") == kind
            ):
                return cached["profile"]
        except (OSError, ValueError, KeyError):
            pass

    text = extract_text(resume_path)

    try:
        profile = build_profile(text, summarizer)
    except Exception:
        # Resume summarisation is optional. Finding jobs is not.
        return build_profile(text, None)

    PROFILE_CACHE.write_text(
        json.dumps(
            {
                "mtime": stamp,
                "kind": kind,
                "profile": profile,
            }
        )
    )

    return profile


def passes_filters(job: dict, f: dict) -> bool:
    """Apply deterministic filters before a job reaches the database/model."""

    title = (job.get("title") or "").strip().lower()
    description = (job.get("description") or "").lower()
    location = (job.get("location") or "").strip().lower()

    # ---------------------------------------------------------------
    # 1. Title relevance
    # ---------------------------------------------------------------

    includes = [
        str(value).strip().lower()
        for value in (f.get("title_include") or [])
        if str(value).strip()
    ]

    excludes = [
        str(value).strip().lower()
        for value in (f.get("title_exclude") or [])
        if str(value).strip()
    ]

    if includes and not any(term in title for term in includes):
        return False

    if any(term in title for term in excludes):
        return False

    # ---------------------------------------------------------------
    # 2. Job type
    # ---------------------------------------------------------------

    allowed_types = {
        str(value).strip().lower()
        for value in (f.get("job_types") or [])
        if str(value).strip()
    }

    job_type = job.get("job_type")

    if not job_type:
        job_type = sources.classify_job_type(
            title,
            description,
        )

    # Only allow configured job types.
    if allowed_types:
        if job_type not in allowed_types:
            return False
    else:
        # Contract/freelance/part-time are excluded by default.
        if job_type in {"contract", "part_time"}:
            return False

    # ---------------------------------------------------------------
    # 3. India eligibility
    # ---------------------------------------------------------------

    eligibility = job.get("india_eligibility")

    if not eligibility:
        eligibility = sources.india_eligibility(
            location,
            description,
        )

    require_india = f.get("require_india_eligibility", True)

    if require_india and eligibility != "eligible":
        return False

    # ---------------------------------------------------------------
    # 4. Location configuration
    # ---------------------------------------------------------------

    configured_locations = [
        str(value).strip().lower()
        for value in (f.get("locations") or [])
        if str(value).strip()
    ]

    if configured_locations:
        location_matches = any(
            value in location
            for value in configured_locations
        )

        remote_allowed = (
            "remote" in configured_locations
            and eligibility == "eligible"
            and "remote" in location
        )

        india_allowed = (
            "india" in configured_locations
            and eligibility == "eligible"
        )

        if not (
            location_matches
            or remote_allowed
            or india_allowed
        ):
            return False

    # ---------------------------------------------------------------
    # 5. Experience / seniority
    # ---------------------------------------------------------------

    experience = job.get("experience_level")

    if not experience:
        experience = sources.experience_level(
            title,
            description,
        )

    if experience == "senior":
        return False

    # ---------------------------------------------------------------
    # 6. Hard seniority markers
    # ---------------------------------------------------------------

    senior_markers = (
        "senior",
        "sr.",
        "sr ",
        "staff ",
        "principal ",
        "lead ",
        "manager",
        "director",
        "head of",
        "vice president",
        "vp ",
        "architect",
    )

    if any(marker in title for marker in senior_markers):
        return False

    return True


# ---------------------------------------------------------------------------


def cmd_scan(
    cfg: dict,
    store: Store,
    quiet: bool = False,
) -> dict:
    all_jobs: list[dict] = []
    errors: list[str] = []
    healthy_companies: list[str] = []

    # ---------------------------------------------------------------
    # Company boards
    # ---------------------------------------------------------------

    for entry in cfg.get("companies", []):
        jobs, err = sources.fetch_company(entry)

        if err:
            errors.append(f"{entry['name']}: {err}")
        else:
            healthy_companies.append(entry["name"])
            all_jobs.extend(jobs)

        if not quiet:
            print(
                f"  {entry['name']:<20} "
                f"{len(jobs):>4} open"
                + (f"  ⚠ {err}" if err else ""),
                flush=True,
            )

    # ---------------------------------------------------------------
    # Discovery aggregators
    # ---------------------------------------------------------------

    discovery = cfg.get("discovery", {})

    if discovery.get("enabled"):
        jobs, discovery_errors = sources.discover(
            discovery.get("queries", []),
            discovery.get("max_per_query", 40),
        )

        all_jobs.extend(jobs)
        errors.extend(discovery_errors)

        if not quiet:
            print(
                f"  {'discovery':<20} "
                f"{len(jobs):>4} found",
                flush=True,
            )

    # ---------------------------------------------------------------
    # Deterministic filtering
    # ---------------------------------------------------------------

    filters = cfg.get("filters", {})

    kept = []

    rejected = {
        "title": 0,
        "location": 0,
        "job_type": 0,
        "experience": 0,
        "other": 0,
    }

    for job in all_jobs:
        title = job.get("title", "")
        location = job.get("location", "")
        description = job.get("description", "")

        before_type = job.get("job_type")

        if not before_type:
            before_type = sources.classify_job_type(
                title,
                description,
            )

        before_location = job.get("india_eligibility")

        if not before_location:
            before_location = sources.india_eligibility(
                location,
                description,
            )

        before_experience = job.get("experience_level")

        if not before_experience:
            before_experience = sources.experience_level(
                title,
                description,
            )

        if passes_filters(job, filters):
            kept.append(job)
            continue

        title_lower = (title or "").lower()

        if any(
            str(term).lower() in title_lower
            for term in (filters.get("title_exclude") or [])
        ):
            rejected["title"] += 1

        elif before_location != "eligible":
            rejected["location"] += 1

        elif before_type in {"contract", "part_time"}:
            rejected["job_type"] += 1

        elif before_experience == "senior":
            rejected["experience"] += 1

        else:
            rejected["other"] += 1

    # Keep descriptions around for scoring.
    desc = {
        job["id"]: job.get("description", "")
        for job in kept
    }

    new = store.upsert(kept)
    closed = store.mark_closed(healthy_companies)

    # ---------------------------------------------------------------
    # Score new/unscored jobs
    # ---------------------------------------------------------------

    llm_cfg = cfg.get("llm", {})
    scored = 0
    scorer = None

    if llm_cfg.get("provider", "none") != "none":
        rows = []
        profile = ""

        pending = store.unscored(
            llm_cfg.get("max_to_score", 5000)
        )

        if pending:
            try:
                scorer = get_scorer(cfg)
                profile = get_profile(cfg, scorer)

            except Exception as exc:
                errors.append(
                    f"scoring unavailable: {type(exc).__name__}"
                )

                if not quiet:
                    print(
                        f"  scoring unavailable "
                        f"({type(exc).__name__}); "
                        "reporting openings unranked",
                        flush=True,
                    )

                scorer = None
                pending = []

            # -------------------------------------------------------
            # Stage 1: local triage
            # -------------------------------------------------------

            floor = int(
                llm_cfg.get("triage_floor", 3)
            )

            if (
                pending
                and llm_cfg.get("triage", True)
                and not isinstance(scorer, LocalScorer)
            ):
                local = LocalScorer(
                    extract_text(
                        ROOT / cfg["resume_path"]
                    )
                )

                rows = []
                settled = 0
                blind = 0

                for row in pending:
                    job = dict(row)

                    job["description"] = (
                        desc.get(row["id"])
                        or row["description"]
                        or ""
                    )

                    if len(job["description"]) < 120:
                        rows.append(row)
                        blind += 1
                        continue

                    s_local, why, breakdown = local.score(
                        profile,
                        job,
                    )

                    if s_local >= floor:
                        rows.append(row)
                    else:
                        store.save_score(
                            row["id"],
                            s_local,
                            f"triage: {why}",
                            breakdown,
                        )
                        settled += 1

                if not quiet:
                    note = (
                        f", {blind} had no description to judge"
                        if blind
                        else ""
                    )

                    print(
                        f"  triage settled {settled} locally, "
                        f"{len(rows)} go to the model{note}",
                        flush=True,
                    )

            else:
                rows = pending

            # -------------------------------------------------------
            # Stage 2: daily budget
            # -------------------------------------------------------

            if rows:
                budget = Budget(
                    store.db,
                    int(
                        llm_cfg.get(
                            "daily_token_limit",
                            500_000,
                        )
                    ),
                )

                size = int(
                    llm_cfg.get("batch_size", 25)
                )

                per_batch = int(
                    llm_cfg.get(
                        "tokens_per_batch",
                        3400,
                    )
                )

                affordable = (
                    max(
                        0,
                        budget.remaining() // per_batch,
                    )
                    * size
                )

                if len(rows) > affordable:
                    if not quiet:
                        print(
                            f"  budget: {budget.report()} "
                            f"-> planning {affordable} "
                            f"of {len(rows)} this run",
                            flush=True,
                        )

                    rows = rows[:affordable]

                elif not quiet:
                    print(
                        f"  budget: {budget.report()}",
                        flush=True,
                    )

            # -------------------------------------------------------
            # Scoring helpers
            # -------------------------------------------------------

            titles = {
                row["id"]: row["title"]
                for row in pending
            }

            def apply(job_id, result):
                nonlocal scored

                score, reason, breakdown = result

                # Seniority cap.
                score, reason = apply_cap(
                    score,
                    titles.get(job_id, ""),
                    reason,
                )

                # Wrong-field relevance cap.
                score, reason = apply_relevance_cap(
                    score,
                    breakdown,
                    reason,
                )

                store.save_score(
                    job_id,
                    score,
                    reason,
                    breakdown,
                )

                scored += 1

            # -------------------------------------------------------
            # Batch scoring
            # -------------------------------------------------------

            if scorer is not None:
                try:
                    if hasattr(scorer, "score_batch"):
                        size = int(
                            llm_cfg.get(
                                "batch_size",
                                getattr(
                                    scorer,
                                    "batch_size",
                                    25,
                                ),
                            )
                        )

                        def run_chunk(chunk):
                            payload = []

                            for row in chunk:
                                job = dict(row)

                                job["description"] = (
                                    desc.get(row["id"])
                                    or row["description"]
                                    or ""
                                )

                                job["job_type"] = (
                                    job.get("job_type")
                                    or sources.classify_job_type(
                                        job.get("title", ""),
                                        job.get("description", ""),
                                    )
                                )

                                job["india_eligibility"] = (
                                    job.get(
                                        "india_eligibility"
                                    )
                                    or sources.india_eligibility(
                                        job.get("location", ""),
                                        job.get("description", ""),
                                    )
                                )

                                job["experience_level"] = (
                                    job.get("experience_level")
                                    or sources.experience_level(
                                        job.get("title", ""),
                                        job.get("description", ""),
                                    )
                                )

                                payload.append(job)

                            return scorer.score_batch(
                                profile,
                                payload,
                            )

                        dropped = []

                        spent_before = getattr(
                            scorer,
                            "tokens_used",
                            0,
                        )

                        for start in range(
                            0,
                            len(rows),
                            size,
                        ):
                            if not budget.can_afford(
                                per_batch
                            ):
                                if not quiet:
                                    print(
                                        f"    budget reached - "
                                        f"{budget.report()}; "
                                        f"{len(rows) - start} jobs "
                                        "wait for the next run",
                                        flush=True,
                                    )
                                break

                            chunk = rows[
                                start:start + size
                            ]

                            try:
                                results = run_chunk(chunk)

                            except (
                                RateLimited,
                                CursorError,
                            ):
                                raise

                            except Exception as exc:
                                if not quiet:
                                    print(
                                        f"    batch failed "
                                        f"({type(exc).__name__}), "
                                        f"deferring {len(chunk)}",
                                        flush=True,
                                    )

                                dropped.extend(chunk)
                                continue

                            finally:
                                now_used = getattr(
                                    scorer,
                                    "tokens_used",
                                    0,
                                )

                                if now_used > spent_before:
                                    budget.record(
                                        now_used
                                        - spent_before
                                    )

                                    spent_before = now_used

                            for row in chunk:
                                result = results.get(
                                    row["id"]
                                )

                                if result is None:
                                    dropped.append(row)
                                else:
                                    apply(
                                        row["id"],
                                        result,
                                    )

                            if not quiet:
                                print(
                                    f"    scored "
                                    f"{scored}/{len(rows)}"
                                    + (
                                        f" ({len(dropped)} dropped)"
                                        if dropped
                                        else ""
                                    ),
                                    flush=True,
                                )

                        # ---------------------------------------------------
                        # Retry dropped jobs in smaller groups.
                        # ---------------------------------------------------

                        if dropped:
                            if not quiet:
                                print(
                                    f"    retrying "
                                    f"{len(dropped)} dropped",
                                    flush=True,
                                )

                            for start in range(
                                0,
                                len(dropped),
                                8,
                            ):
                                if not budget.can_afford(
                                    per_batch
                                ):
                                    break

                                chunk = dropped[
                                    start:start + 8
                                ]

                                try:
                                    results = run_chunk(
                                        chunk
                                    )

                                except Exception:
                                    break

                                finally:
                                    now_used = getattr(
                                        scorer,
                                        "tokens_used",
                                        0,
                                    )

                                    if now_used > spent_before:
                                        budget.record(
                                            now_used
                                            - spent_before
                                        )

                                        spent_before = now_used

                                for row in chunk:
                                    result = results.get(
                                        row["id"]
                                    )

                                    if result is not None:
                                        apply(
                                            row["id"],
                                            result,
                                        )

                    # -------------------------------------------------------
                    # Non-batch scoring
                    # -------------------------------------------------------

                    else:
                        workers = int(
                            llm_cfg.get(
                                "concurrency",
                                3,
                            )
                        )

                        def work(row):
                            job = dict(row)

                            job["description"] = (
                                desc.get(row["id"])
                                or row["description"]
                                or ""
                            )

                            job["job_type"] = (
                                job.get("job_type")
                                or sources.classify_job_type(
                                    job.get("title", ""),
                                    job.get("description", ""),
                                )
                            )

                            job["india_eligibility"] = (
                                job.get(
                                    "india_eligibility"
                                )
                                or sources.india_eligibility(
                                    job.get("location", ""),
                                    job.get("description", ""),
                                )
                            )

                            job["experience_level"] = (
                                job.get(
                                    "experience_level"
                                )
                                or sources.experience_level(
                                    job.get("title", ""),
                                    job.get("description", ""),
                                )
                            )

                            return (
                                row,
                                scorer.score(
                                    profile,
                                    job,
                                ),
                            )

                        with ThreadPoolExecutor(
                            max_workers=workers
                        ) as pool:

                            for row, result in pool.map(
                                work,
                                rows,
                            ):
                                apply(
                                    row["id"],
                                    result,
                                )

                                if not quiet:
                                    print(
                                        f"    scored "
                                        f"{result[0]}/10  "
                                        f"{row['title'][:55]}",
                                        flush=True,
                                    )

                except (
                    RateLimited,
                    CursorError,
                ) as exc:

                    errors.append(
                        f"scoring stopped early: {exc}"
                    )

                    if not quiet:
                        print(
                            f"    ! {exc} - "
                            f"{scored} scored, "
                            "rest will wait for the next run",
                            flush=True,
                        )

    # ---------------------------------------------------------------
    # Run logging
    # ---------------------------------------------------------------

    store.log_run(
        len(new),
        len(closed),
        errors,
    )

    if not quiet:
        cost = ""

        if (
            scored
            and scorer is not None
            and hasattr(scorer, "tokens_used")
        ):
            cost = (
                f" · {scorer.tokens_used:,} tokens"
            )

        elif (
            scored
            and scorer is not None
            and hasattr(scorer, "cost_usd")
        ):
            cost = (
                f" · ${scorer.cost_usd():.4f}"
            )

        print(
            f"\n"
            f"{len(all_jobs)} discovered · "
            f"{len(kept)} passed filters · "
            f"{len(new)} new · "
            f"{len(closed)} closed · "
            f"{scored} scored · "
            f"{len(errors)} errors"
            f"{cost}"
        )

        print(
            "  Rejected: "
            f"title={rejected['title']} · "
            f"location={rejected['location']} · "
            f"type={rejected['job_type']} · "
            f"experience={rejected['experience']} · "
            f"other={rejected['other']}",
            flush=True,
        )

        for error in errors:
            print(
                f"  ERROR: {error}",
                flush=True,
            )

    return {
        "new": len(new),
        "closed": len(closed),
        "errors": errors,
    }


def cmd_digest(
    cfg: dict,
    store: Store,
    days: int,
    dry_run: bool,
) -> None:

    llm_cfg = cfg.get("llm", {})

    min_score = llm_cfg.get(
        "min_score",
        0,
    )

    scoring_on = (
        llm_cfg.get("provider", "none")
        not in ("none", None, "")
    )

    pending = store.unnotified(
        min_score,
        require_scored=scoring_on,
    )

    closed = store.closed_since(
        days * 86400
    )

    cap = int(
        cfg.get("digest", {}).get(
            "max_jobs",
            60,
        )
    )

    new = pending[:cap]
    overflow = len(pending) - len(new)

    label = time.strftime(
        "Week of %d %b %Y"
    )

    notes = []

    if overflow > 0:
        notes.append(
            f"Showing the top {len(new)} of "
            f"{len(pending)} tracked openings; "
            f"{overflow} more are on file."
        )

    html, text = digest_mod.render(
        new,
        closed,
        label,
        notes,
    )

    if dry_run:
        out = ROOT / "digest_preview.html"
        out.write_text(html)

        print(text)
        print(
            f"\nPreview written to {out}"
        )
        return

    if not pending and not closed:
        html, text = digest_mod.render(
            [],
            [],
            time.strftime(
                "Week of %d %b %Y"
            ),
            [
                "No new matching opportunities today."
            ],
        )

        subject = (
            "Job Radar — No new matching "
            "opportunities today"
        )

        digest_mod.send(
            cfg["email"],
            subject,
            html,
            text,
        )

        print(
            f"Sent to {cfg['email']['to']}: "
            "no new matching opportunities today."
        )

        return

    count = len(pending)

    subject = (
        f"Job Radar — {count} new opening"
        f"{'s' if count != 1 else ''}"
    )

    digest_mod.send(
        cfg["email"],
        subject,
        html,
        text,
    )

    store.mark_notified(
        [row["id"] for row in pending]
    )

    print(
        f"Sent to {cfg['email']['to']}: "
        f"{len(new)} shown of {count} new, "
        f"{len(closed)} closed."
    )


def cmd_alert(
    cfg: dict,
    store: Store,
    dry_run: bool,
) -> None:

    """Check prioritised companies for new openings."""

    watch = alerts_mod.watched_names(
        cfg
    )

    narrow = dict(cfg)

    narrow["companies"] = [
        entry
        for entry in cfg.get("companies", [])
        if entry["name"] in watch
    ]

    narrow["discovery"] = {
        "enabled": False
    }

    cmd_scan(
        narrow,
        store,
        quiet=True,
    )

    baselined = store.baselined_companies()

    pending = store.unalerted()

    rows = alerts_mod.pick(
        pending,
        cfg,
        baselined,
    )

    fresh = {
        row["company"]
        for row in pending
    } - baselined

    if fresh and not dry_run:
        print(
            f"Baselining {len(fresh)} new "
            f"compan(y/ies): "
            f"{', '.join(sorted(fresh))[:100]} "
            "— alerts start from their next opening."
        )

    if not rows:
        print(
            "Nothing new at watched companies."
        )

        store.mark_alerted(
            [
                row["id"]
                for row in store.unalerted()
            ]
        )

        return

    cap = int(
        cfg.get("alerts", {}).get(
            "max_per_alert",
            15,
        )
    )

    label = time.strftime(
        "%d %b %Y, %H:%M"
    )

    if dry_run:
        html, text = alerts_mod.render(
            rows[:cap],
            label,
        )

        preview = ROOT / "alert_preview.html"

        preview.write_text(html)

        print(text or "(nothing)")

        print(
            f"\n{len(rows)} would alert; "
            f"preview at {preview}"
        )

        return

    alerts_mod.send(
        cfg,
        rows[:cap],
        label,
    )

    store.mark_alerted(
        [
            row["id"]
            for row in store.unalerted()
        ]
    )

    print(
        f"Alerted {len(rows)} new opening(s) "
        "at watched companies."
    )


def cmd_recap(
    store: Store,
    dry_run: bool,
) -> None:

    """Re-apply seniority and relevance caps."""

    rows = store.db.execute(
        """
        SELECT id, title, score, reason, breakdown
        FROM jobs
        WHERE score IS NOT NULL
        """
    ).fetchall()

    changed = []

    for row in rows:
        try:
            breakdown = (
                json.loads(row["breakdown"])
                if row["breakdown"]
                else {}
            )
        except (TypeError, ValueError):
            breakdown = {}

        new_score, new_reason = apply_cap(
            row["score"],
            row["title"],
            row["reason"] or "",
        )

        new_score, new_reason = (
            apply_relevance_cap(
                new_score,
                breakdown,
                new_reason,
            )
        )

        if new_score != row["score"]:
            changed.append(
                (
                    row["id"],
                    new_score,
                    new_reason,
                    row["title"],
                    row["score"],
                )
            )

    if dry_run:
        for (
            _,
            new_score,
            _,
            title,
            old_score,
        ) in changed[:15]:

            print(
                f"  {old_score} -> "
                f"{new_score}  "
                f"{title[:60]}"
            )

        print(
            f"\n{len(changed)} of "
            f"{len(rows)} scores would change "
            "(0 tokens)"
        )

        return

    for (
        job_id,
        new_score,
        new_reason,
        _,
        _,
    ) in changed:

        store.db.execute(
            """
            UPDATE jobs
            SET score = ?, reason = ?
            WHERE id = ?
            """,
            (
                new_score,
                new_reason,
                job_id,
            ),
        )

    store.db.commit()

    print(
        f"Capped {len(changed)} jobs "
        "(senior title or wrong field). "
        "No tokens used."
    )


def cmd_list(
    store: Store,
    days: int,
) -> None:

    rows = store.since(
        days * 86400
    )

    if not rows:
        print("Nothing new.")
        return

    for row in rows:
        score = (
            f"{row['score']}/10"
            if row["score"] is not None
            else "  - "
        )

        print(
            f"[{score}] "
            f"{row['title'][:60]:<60} "
            f"{row['company'][:18]:<18} "
            f"{row['url']}"
        )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="jobradar"
    )

    parser.add_argument(
        "--config",
        default=str(
            ROOT / "config.yaml"
        ),
    )

    sub = parser.add_subparsers(
        dest="cmd",
        required=True,
    )

    sub.add_parser("scan")

    digest_parser = sub.add_parser(
        "digest"
    )
    digest_parser.add_argument(
        "--days",
        type=int,
        default=7,
    )
    digest_parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    run_parser = sub.add_parser("run")
    run_parser.add_argument(
        "--days",
        type=int,
        default=7,
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    list_parser = sub.add_parser("list")
    list_parser.add_argument(
        "--days",
        type=int,
        default=7,
    )

    alert_parser = sub.add_parser(
        "alert"
    )
    alert_parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    recap_parser = sub.add_parser(
        "recap"
    )
    recap_parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    sub.add_parser("test")

    args = parser.parse_args(argv)

    cfg = load_config(
        Path(args.config)
    )

    store = Store(
        ROOT / "jobradar.db"
    )

    if args.cmd == "scan":
        cmd_scan(cfg, store)

    elif args.cmd == "digest":
        cmd_digest(
            cfg,
            store,
            args.days,
            args.dry_run,
        )

    elif args.cmd == "run":
        cmd_scan(cfg, store)
        cmd_digest(
            cfg,
            store,
            args.days,
            args.dry_run,
        )

    elif args.cmd == "list":
        cmd_list(
            store,
            args.days,
        )

    elif args.cmd == "recap":
        cmd_recap(
            store,
            args.dry_run,
        )

    elif args.cmd == "alert":
        cmd_alert(
            cfg,
            store,
            args.dry_run,
        )

    elif args.cmd == "test":
        digest_mod.send(
            cfg["email"],
            "Job Radar — test",
            "<p>SMTP works. Your JobRadar email is working.</p>",
            "SMTP works.",
        )

        print(
            f"Test email sent to "
            f"{cfg['email']['to']}."
        )


if __name__ == "__main__":
    main()
