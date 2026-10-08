"""Job source adapters and discovery helpers.

Every adapter returns a normalized job dictionary.

This module is intentionally defensive:
- one broken source must not stop the whole run
- HTTP requests are retried
- jobs carry job_type / eligibility metadata
- aggregator discovery does structured local matching
- India eligibility is kept separate from generic "remote"
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import requests


UA = {
    "User-Agent": "jobradar/0.2 (personal job tracker)"
}

TIMEOUT = 25
MAX_RETRIES = 3


# --------------------------------------------------------------------------
# General helpers
# --------------------------------------------------------------------------

def _job_id(company: str, title: str, url: str) -> str:
    """Stable ID used for deduplication across runs."""
    return hashlib.sha1(
        f"{company}|{title}|{url}".encode("utf-8")
    ).hexdigest()[:16]


def _clean_url(url: str | None) -> str:
    """Remove common tracking parameters while preserving the real URL."""
    if not url:
        return ""

    try:
        parts = urlsplit(url)

        if not parts.scheme or not parts.netloc:
            return url.strip()

        # Keep meaningful query parameters, remove common tracking ones.
        if parts.query:
            kept = []

            for item in parts.query.split("&"):
                if "=" in item:
                    key, value = item.split("=", 1)
                else:
                    key, value = item, ""

                key_lower = key.lower()

                if key_lower.startswith("utm_"):
                    continue

                if key_lower in {
                    "source",
                    "src",
                    "ref",
                    "referrer",
                    "tracking",
                    "trk",
                }:
                    continue

                kept.append(f"{key}={value}" if value else key)

            query = "&".join(kept)
        else:
            query = ""

        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                query,
                "",
            )
        )

    except Exception:
        return url.strip()


def _text(*values: Any) -> str:
    """Safely combine arbitrary values into searchable text."""
    return " ".join(
        str(v).strip()
        for v in values
        if v not in (None, "")
    )


def _epoch(value) -> float | None:
    """Normalize milliseconds, seconds, ISO dates, or missing values."""
    if value in (None, ""):
        return None

    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)

    try:
        from datetime import datetime

        s = str(value).strip()

        if s.endswith("Z"):
            s = s[:-1] + "+00:00"

        return datetime.fromisoformat(s).timestamp()

    except Exception:
        return None


def _get(url: str) -> Any:
    """GET JSON with small exponential backoff."""
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(
                url,
                headers=UA,
                timeout=TIMEOUT,
            )

            response.raise_for_status()
            return response.json()

        except Exception as exc:  # noqa: BLE001
            last_error = exc

            if attempt < MAX_RETRIES - 1:
                time.sleep(1.5 * (2 ** attempt))

    raise last_error


def _post(url: str, body: dict) -> Any:
    """POST JSON with small exponential backoff."""
    last_error = None

    headers = {
        **UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=TIMEOUT,
            )

            response.raise_for_status()
            return response.json()

        except Exception as exc:  # noqa: BLE001
            last_error = exc

            if attempt < MAX_RETRIES - 1:
                time.sleep(1.5 * (2 ** attempt))

    raise last_error


# --------------------------------------------------------------------------
# Job classification
# --------------------------------------------------------------------------

INTERNSHIP_PATTERNS = (
    r"\bintern\b",
    r"\binternship\b",
    r"\bco[- ]?op\b",
    r"\bapprentice\b",
    r"\buniversity intern\b",
    r"\bstudent intern\b",
)


FULL_TIME_PATTERNS = (
    r"\bfull[- ]?time\b",
    r"\bsoftware engineer\b",
    r"\bsoftware developer\b",
    r"\bdeveloper\b",
    r"\bengineer\b",
    r"\bprogrammer\b",
    r"\bgraduate engineer\b",
    r"\bgraduate trainee\b",
    r"\bassociate engineer\b",
    r"\bassociate software\b",
    r"\btrainee engineer\b",
    r"\bjunior engineer\b",
    r"\bjunior developer\b",
    r"\bentry[- ]level\b",
    r"\bnew grad\b",
    r"\bnew graduate\b",
)


CONTRACT_PATTERNS = (
    r"\bcontract\b",
    r"\bcontractor\b",
    r"\bfreelance\b",
    r"\bfreelancer\b",
)


PART_TIME_PATTERNS = (
    r"\bpart[- ]?time\b",
)


def classify_job_type(
    title: str,
    description: str = "",
    explicit_type: str | None = None,
) -> str:
    """Return internship/full_time/contract/part_time/unknown."""

    explicit = (explicit_type or "").strip().lower()

    if explicit in {
        "internship",
        "intern",
    }:
        return "internship"

    if explicit in {
        "full_time",
        "full-time",
        "full time",
    }:
        return "full_time"

    if explicit in {
        "contract",
        "contractor",
    }:
        return "contract"

    if explicit in {
        "part_time",
        "part-time",
        "part time",
    }:
        return "part_time"

    text = _text(title, description).lower()

    if any(re.search(pattern, text) for pattern in INTERNSHIP_PATTERNS):
        return "internship"

    if any(re.search(pattern, text) for pattern in CONTRACT_PATTERNS):
        return "contract"

    if any(re.search(pattern, text) for pattern in PART_TIME_PATTERNS):
        return "part_time"

    if any(re.search(pattern, text) for pattern in FULL_TIME_PATTERNS):
        return "full_time"

    return "unknown"


# --------------------------------------------------------------------------
# Location / India eligibility
# --------------------------------------------------------------------------

INDIA_LOCATIONS = (
    "india",
    "pune",
    "bangalore",
    "bengaluru",
    "hyderabad",
    "mumbai",
    "gurgaon",
    "gurugram",
    "noida",
    "new delhi",
    "delhi",
    "chennai",
    "kolkata",
    "ahmedabad",
    "jaipur",
    "kochi",
    "chandigarh",
    "indore",
    "bhubaneswar",
    "remote - india",
    "india - remote",
    "remote india",
)


WORLDWIDE_REMOTE = (
    "worldwide",
    "anywhere in the world",
    "work from anywhere",
    "global remote",
    "remote worldwide",
    "anywhere",
)


NON_INDIA_EXCLUSIVE = (
    "united states only",
    "usa only",
    "us only",
    "u.s. only",
    "canada only",
    "uk only",
    "united kingdom only",
    "europe only",
    "australia only",
    "new zealand only",
    "us/canada only",
    "north america only",
)


def india_eligibility(location: str, description: str = "") -> str:
    """Return eligible / ineligible / unknown for India."""

    text = _text(location, description).lower()

    # Strong India signal.
    if any(term in text for term in INDIA_LOCATIONS):
        return "eligible"

    # Explicit worldwide remote is normally acceptable.
    if any(term in text for term in WORLDWIDE_REMOTE):
        return "eligible"

    # Explicitly restricted to another region.
    if any(term in text for term in NON_INDIA_EXCLUSIVE):
        return "ineligible"

    # If the source only says "remote", we do NOT assume India.
    if re.search(r"\bremote\b", text):
        return "unknown"

    # No location information.
    if not location.strip():
        return "unknown"

    return "ineligible"


# --------------------------------------------------------------------------
# Experience detection
# --------------------------------------------------------------------------

SENIOR_PATTERNS = (
    r"\bsenior\b",
    r"\bsr\.?\b",
    r"\bstaff\b",
    r"\bprincipal\b",
    r"\blead\b",
    r"\bmanager\b",
    r"\bdirector\b",
    r"\bhead of\b",
    r"\bvice president\b",
    r"\bvp\b",
    r"\barchitect\b",
    r"\bdistinguished\b",
)


JUNIOR_PATTERNS = (
    r"\bintern\b",
    r"\binternship\b",
    r"\bjunior\b",
    r"\bjr\.?\b",
    r"\bentry[- ]level\b",
    r"\bnew grad\b",
    r"\bnew graduate\b",
    r"\bgraduate\b",
    r"\btrainee\b",
    r"\bassociate\b",
    r"\bapprentice\b",
    r"\bfresher\b",
    r"\buniversity\b",
    r"\bcampus\b",
)


def _minimum_years(text: str) -> float | None:
    """Try to determine the minimum stated experience requirement."""

    text = text.lower()

    patterns = (
        r"(\d+(?:\.\d+)?)\s*\+\s*(?:years?|yrs?)",
        r"minimum\s+(?:of\s+)?(\d+(?:\.\d+)?)\s*(?:years?|yrs?)",
        r"at least\s+(\d+(?:\.\d+)?)\s*(?:years?|yrs?)",
        r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)",
    )

    values = []

    for pattern in patterns:
        for match in re.finditer(pattern, text):
            try:
                values.append(float(match.group(1)))
            except (ValueError, TypeError):
                pass

    return min(values) if values else None


def experience_level(title: str, description: str = "") -> str:
    """Return junior / acceptable / senior / unknown."""

    title_text = title.lower()
    text = _text(title, description).lower()

    if any(re.search(pattern, title_text) for pattern in SENIOR_PATTERNS):
        # Explicit junior marker in the title wins for things like
        # "Senior Software Engineer Intern", but obvious leadership roles don't.
        if not any(re.search(pattern, title_text) for pattern in JUNIOR_PATTERNS):
            return "senior"

    years = _minimum_years(text)

    if years is not None:
        if years > 2:
            return "senior"

        if years <= 2:
            return "acceptable"

    if any(re.search(pattern, text) for pattern in JUNIOR_PATTERNS):
        return "junior"

    return "unknown"


# --------------------------------------------------------------------------
# Salary extraction
# --------------------------------------------------------------------------

def extract_salary(text: str) -> str:
    """Extract a small useful salary snippet when explicitly stated."""

    if not text:
        return ""

    patterns = (
        r"(?:₹|INR)\s?[\d,.]+\s?(?:LPA|lakhs?|lakh)?",
        r"\$[\d,.]+(?:\s?-\s?\$?[\d,.]+)?",
        r"₹\s?[\d,.]+\s?(?:per month|/month|monthly)",
        r"\b\d+(?:\.\d+)?\s?LPA\b",
    )

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return match.group(0).strip()

    return ""


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

def _norm(
    company,
    title,
    url,
    location="",
    posted_at=None,
    description="",
    source="",
    domain="",
    job_type=None,
    salary="",
    tags=None,
):
    title = (title or "").strip()
    description = (description or "").strip()
    location = (location or "").strip()
    url = _clean_url(url)

    return {
        "id": _job_id(company or "?", title, url),
        "company": (company or "?").strip(),
        "title": title,
        "url": url,
        "location": location,
        "posted_at": posted_at,
        "description": description[:6000],
        "source": source,
        "domain": domain or "",

        # New metadata.
        "job_type": classify_job_type(
            title,
            description,
            explicit_type=job_type,
        ),
        "salary": salary or extract_salary(description),
        "tags": tags or [],
        "india_eligibility": india_eligibility(
            location,
            description,
        ),
        "experience_level": experience_level(
            title,
            description,
        ),
    }


# --------------------------------------------------------------------------
# Per-company boards
# --------------------------------------------------------------------------

def greenhouse(name: str, slug: str) -> list[dict]:
    data = _get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    )

    out = []

    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        description = j.get("content", "")

        out.append(
            _norm(
                name,
                j.get("title"),
                j.get("absolute_url"),
                loc,
                _epoch(
                    j.get("updated_at")
                    or j.get("first_published")
                ),
                description,
                "greenhouse",
            )
        )

    return out


def lever(name: str, slug: str) -> list[dict]:
    data = _get(
        f"https://api.lever.co/v0/postings/{slug}?mode=json"
    )

    out = []

    for j in data:
        cats = j.get("categories") or {}
        description = j.get("descriptionPlain", "")

        out.append(
            _norm(
                name,
                j.get("text"),
                j.get("hostedUrl"),
                cats.get("location", ""),
                _epoch(j.get("createdAt")),
                description,
                "lever",
                tags=cats.get("team") or [],
            )
        )

    return out


def ashby(name: str, slug: str) -> list[dict]:
    data = _get(
        f"https://api.ashbyhq.com/posting-api/job-board/"
        f"{slug}?includeCompensation=true"
    )

    out = []

    for j in data.get("jobs", []):
        description = j.get("descriptionPlain", "")

        compensation = (
            j.get("compensation")
            or j.get("compensationTierSummary")
            or ""
        )

        out.append(
            _norm(
                name,
                j.get("title"),
                j.get("jobUrl"),
                j.get("location", ""),
                _epoch(j.get("publishedAt")),
                description,
                "ashby",
                salary=str(compensation),
            )
        )

    return out


def workable(name: str, slug: str) -> list[dict]:
    data = _get(
        f"https://apply.workable.com/api/v1/widget/accounts/"
        f"{slug}?details=true"
    )

    out = []

    for j in data.get("jobs", []):
        out.append(
            _norm(
                name,
                j.get("title"),
                j.get("url") or j.get("application_url"),
                j.get("location", ""),
                _epoch(j.get("published_on")),
                j.get("description", ""),
                "workable",
            )
        )

    return out


BOARDS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workable": workable,
}


def fetch_company(entry: dict) -> tuple[list[dict], str | None]:
    """Fetch one company without allowing failures to kill the run."""

    board = entry.get("board", "greenhouse").lower()
    fn = BOARDS.get(board)

    if not fn:
        return [], f"unknown board '{board}'"

    try:
        jobs = fn(entry["name"], entry["slug"])

        for job in jobs:
            job["domain"] = entry.get("domain", "")

        return jobs, None

    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Aggregators
# --------------------------------------------------------------------------

def remotive(query: str = "", limit: int = 40) -> list[dict]:
    """Fetch remote jobs from Remotive.

    Remotive is remote-focused, so India eligibility is determined locally
    from candidate_required_location.
    """

    params = f"?limit={limit}"

    if query:
        params += f"&search={quote(query)}"

    data = _get(
        f"https://remotive.com/api/remote-jobs{params}"
    )

    out = []

    for j in data.get("jobs", [])[:limit]:
        out.append(
            _norm(
                j.get("company_name", "?"),
                j.get("title"),
                j.get("url"),
                j.get(
                    "candidate_required_location",
                    "Remote",
                ),
                _epoch(j.get("publication_date")),
                j.get("description", ""),
                "remotive",
                job_type=j.get("job_type"),
                salary=j.get("salary", ""),
                tags=j.get("tags") or [],
            )
        )

    return out


def arbeitnow(query: str = "", limit: int = 40) -> list[dict]:
    """Fetch Arbeitnow jobs once and filter locally."""

    data = _get(
        "https://www.arbeitnow.com/api/job-board-api"
    )

    query_terms = _query_terms(query)
    out = []

    for j in data.get("data", []):
        title = j.get("title", "")
        tags = j.get("tags") or []
        description = j.get("description", "")
        location = j.get("location", "")

        haystack = _text(
            title,
            " ".join(tags),
            description,
            location,
        ).lower()

        if query_terms and not _matches_query(
            query_terms,
            haystack,
        ):
            continue

        out.append(
            _norm(
                j.get("company_name", "?"),
                title,
                j.get("url"),
                location,
                _epoch(j.get("created_at")),
                description,
                "arbeitnow",
                tags=tags,
            )
        )

        if len(out) >= limit:
            break

    return out


# --------------------------------------------------------------------------
# Structured aggregator matching
# --------------------------------------------------------------------------

STOP_WORDS = {
    "a",
    "an",
    "and",
    "at",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "years",
    "year",
    "india",
    "remote",
}


def _query_terms(query: str) -> list[str]:
    """Turn a query into useful search concepts."""

    if not query:
        return []

    words = re.findall(
        r"[a-zA-Z0-9+#.]+",
        query.lower(),
    )

    return [
        word
        for word in words
        if len(word) > 1
        and word not in STOP_WORDS
    ]


def _matches_query(
    query_terms: list[str],
    haystack: str,
) -> bool:
    """Require meaningful query terms instead of OR-ing every word.

    We deliberately require a majority of the meaningful terms. This prevents
    a query such as "python backend engineer" from matching a random job merely
    because the description contains the word "engineer".
    """

    if not query_terms:
        return True

    matches = sum(
        1
        for term in query_terms
        if term in haystack
    )

    required = max(
        1,
        int(round(len(query_terms) * 0.60)),
    )

    return matches >= required


def _query_is_internship(query: str) -> bool:
    text = query.lower()

    return any(
        re.search(pattern, text)
        for pattern in INTERNSHIP_PATTERNS
    )


def _query_is_full_time(query: str) -> bool:
    text = query.lower()

    return any(
        re.search(pattern, text)
        for pattern in FULL_TIME_PATTERNS
    )


def _filter_discovery_job(
    job: dict,
    query: str,
) -> bool:
    """Apply high-confidence discovery filters."""

    title = job.get("title", "").lower()
    description = job.get("description", "")
    location = job.get("location", "")

    haystack = _text(
        title,
        description,
        location,
    ).lower()

    terms = _query_terms(query)

    # Skill/title relevance.
    if terms and not _matches_query(
        terms,
        haystack,
    ):
        return False

    # User's main market: India-eligible jobs.
    eligibility = job.get(
        "india_eligibility",
        india_eligibility(
            location,
            description,
        ),
    )

    if eligibility != "eligible":
        return False

    # Only internship/full-time/unknown are candidates for the main feed.
    job_type = job.get("job_type", "unknown")

    if job_type in {
        "contract",
        "part_time",
    }:
        return False

    # Internship query must actually produce an internship.
    if _query_is_internship(query):
        if job_type != "internship":
            return False

    # Full-time query must not accidentally return internships.
    if _query_is_full_time(query):
        if job_type == "internship":
            return False

    # Reject obvious seniority.
    level = job.get("experience_level", "unknown")

    if level == "senior":
        return False

    return True


def discover(
    queries: list[str],
    max_per_query: int = 40,
) -> tuple[list[dict], list[str]]:
    """Discover jobs from aggregators.

    Each provider is fetched once per run to avoid excessive API requests.
    Individual provider failures are returned as errors rather than raising.
    """

    jobs: list[dict] = []
    errors: list[str] = []

    source_jobs: dict[str, list[dict]] = {}

    providers = (
        ("remotive", remotive),
        ("arbeitnow", arbeitnow),
    )

    for source_name, fn in providers:
        try:
            # Fetch a reasonable pool once.
            fetch_limit = max(
                max_per_query * max(1, len(queries)),
                100,
            )

            source_jobs[source_name] = fn(
                "",
                fetch_limit,
            )

        except Exception as exc:  # noqa: BLE001
            errors.append(
                f"{source_name}: "
                f"{type(exc).__name__}: {exc}"
            )

            source_jobs[source_name] = []

        # Avoid hammering free endpoints.
        time.sleep(1.0)

    for query in queries:
        query_count = 0

        for source_name, source_results in source_jobs.items():

            for job in source_results:

                if not _filter_discovery_job(
                    job,
                    query,
                ):
                    continue

                jobs.append(job)
                query_count += 1

                if query_count >= max_per_query:
                    break

            if query_count >= max_per_query:
                break

    # Deduplicate while preserving insertion order.
    unique: dict[str, dict] = {}

    for job in jobs:
        unique[job["id"]] = job

    return list(unique.values()), errors


# --------------------------------------------------------------------------
# Enterprise boards
# --------------------------------------------------------------------------

def workday(name: str, slug: str) -> list[dict]:
    """slug is tenant/wdN/site."""

    parts = slug.split("/")

    if len(parts) != 3:
        raise ValueError(
            "Workday slug must be tenant/wdN/site"
        )

    tenant, wd, site = parts

    base = (
        f"https://{tenant}.{wd}.myworkdayjobs.com"
    )

    api = (
        f"{base}/wday/cxs/"
        f"{tenant}/{site}/jobs"
    )

    out: list[dict] = []

    for offset in range(0, 200, 20):
        data = _post(
            api,
            {
                "appliedFacets": {},
                "limit": 20,
                "offset": offset,
                "searchText": "",
            },
        )

        postings = data.get("jobPostings") or []

        if not postings:
            break

        for j in postings:
            path = j.get("externalPath") or ""

            description = (
                " ".join(
                    j.get("bulletFields") or []
                )
            )

            out.append(
                _norm(
                    name,
                    j.get("title"),
                    f"{base}/{site}{path}",
                    j.get("locationsText", ""),
                    None,
                    description,
                    "workday",
                )
            )

        if len(postings) < 20:
            break

    return out


def oracle_cloud(name: str, slug: str) -> list[dict]:
    """slug is host/siteNumber."""

    host, site = slug.split("/", 1)

    out: list[dict] = []

    for offset in (0, 200, 400):
        url = (
            f"https://{host}"
            "/hcmRestApi/resources/latest/"
            "recruitingCEJobRequisitions"
            "?onlyData=true"
            "&expand=requisitionList.secondaryLocations"
            f"&finder=findReqs;siteNumber={site},"
            f"limit=200,offset={offset}"
        )

        data = _get(url)

        items = (
            data.get("items")
            or [{}]
        )

        reqs = (
            items[0].get("requisitionList")
            or []
        )

        if not reqs:
            break

        for j in reqs:
            rid = j.get("Id") or ""

            description = (
                j.get("ShortDescriptionStr")
                or ""
            )

            out.append(
                _norm(
                    name,
                    j.get("Title"),
                    (
                        f"https://{host}"
                        f"/hcmUI/CandidateExperience"
                        f"/en/sites/{site}/job/{rid}"
                    ),
                    j.get("PrimaryLocation") or "",
                    _epoch(j.get("PostedDate")),
                    description,
                    "oracle",
                )
            )

        if len(reqs) < 200:
            break

    return out


def atlassian_board(
    name: str,
    slug: str,
) -> list[dict]:
    """Atlassian's public career listing endpoint."""

    data = _get(
        "https://www.atlassian.com/"
        "endpoint/careers/listings"
    )

    out = []

    for j in data:
        portal = (
            j.get("portalJobPost")
            or {}
        )

        locs = j.get("locations") or []

        description = re.sub(
            r"<[^>]+>",
            " ",
            j.get("overview") or "",
        )

        out.append(
            _norm(
                name,
                j.get("title"),
                portal.get("portalUrl"),
                "; ".join(locs[:2]),
                _epoch(
                    portal.get("updatedDate")
                ),
                description,
                "atlassian",
            )
        )

    return out


BOARDS.update(
    {
        "workday": workday,
        "oracle": oracle_cloud,
        "atlassian": atlassian_board,
    }
)
