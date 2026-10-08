"""Render the JobRadar digest and send it over SMTP."""

from __future__ import annotations

import os
import smtplib
import time
from email.message import EmailMessage
from html import escape


CSS = """
body {
  font: 15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  color: #1a1a1a;
  background: #f6f7f9;
  margin: 0;
  padding: 24px;
}

.wrap {
  max-width: 640px;
  margin: 0 auto;
  background: #fff;
  border-radius: 12px;
  padding: 28px;
  border: 1px solid #e3e5e9;
}

h1 {
  font-size: 20px;
  margin: 0 0 4px;
}

.sub {
  color: #6b7280;
  font-size: 13px;
  margin: 0 0 24px;
}

h2 {
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: .06em;
  color: #6b7280;
  margin: 28px 0 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid #eceef2;
}

.job {
  padding: 12px 0;
  border-bottom: 1px solid #f1f2f5;
}

.job:last-child {
  border-bottom: none;
}

.t {
  font-weight: 600;
  text-decoration: none;
  color: #0b5cff;
}

.m {
  color: #6b7280;
  font-size: 13px;
  margin-top: 3px;
}

.score {
  display: inline-block;
  background: #eef4ff;
  color: #0b5cff;
  border-radius: 5px;
  padding: 1px 7px;
  font-size: 12px;
  font-weight: 600;
  margin-right: 6px;
}

.why {
  color: #4b5563;
  font-size: 13px;
  font-style: italic;
  margin-top: 3px;
}

.empty {
  color: #9ca3af;
  font-size: 14px;
}

.foot {
  color: #9ca3af;
  font-size: 12px;
  margin-top: 28px;
  border-top: 1px solid #eceef2;
  padding-top: 12px;
}

.closed {
  color: #9ca3af;
  font-size: 13px;
  padding: 4px 0;
}
"""


def _job_html(r) -> str:
    score = (
        f'<span class="score">{r["score"]}/10</span>'
        if r["score"] is not None
        else ""
    )

    job_type = (
        r["job_type"] or "unknown"
    ).replace("_", " ").title()

    salary = r["salary"] or "Salary not listed"

    why = (
        f'<div class="why">{escape(r["reason"])}</div>'
        if r["reason"]
        else ""
    )

    age = ""

    if r["posted_at"]:
        hours = int(
            (time.time() - r["posted_at"]) / 3600
        )

        if hours < 1:
            age = " · posted <1h ago"
        elif hours < 24:
            age = f" · posted {hours}h ago"
        else:
            days = hours // 24
            age = f" · posted {days}d ago"

    apply_url = escape(
        r["url"] or "#"
    )

    return f"""<div class="job">
      {score}
      <a class="t" href="{apply_url}">
        {escape(r["title"])}
      </a>

      <div class="m">
        {escape(r["company"])}
        · {escape(r["location"] or "location n/a")}
        · {escape(job_type)}
        {age}
      </div>

      <div class="m">
        Salary: {escape(salary)}
      </div>

      {why}

      <div class="m">
        <a href="{apply_url}">
          Apply / View official posting →
        </a>
      </div>
    </div>"""


def render(
    new_jobs,
    closed_jobs,
    period_label: str,
    notes: list[str],
) -> tuple[str, str]:

    internships = [
        r
        for r in new_jobs
        if (r["job_type"] or "").lower()
        == "internship"
    ]

    full_time = [
        r
        for r in new_jobs
        if (r["job_type"] or "").lower()
        == "full_time"
    ]

    other = [
        r
        for r in new_jobs
        if (r["job_type"] or "").lower()
        not in ("internship", "full_time")
    ]

    def block(rows, empty_msg):
        return (
            "".join(
                _job_html(r)
                for r in rows
            )
            or f'<p class="empty">{empty_msg}</p>'
        )

    closed_html = "".join(
        f'<div class="closed">✕ '
        f'{escape(r["title"])} — '
        f'{escape(r["company"])}</div>'
        for r in closed_jobs
    ) or '<p class="empty">Nothing closed.</p>'

    err_html = ""

    if notes:
        err_html = (
            '<p class="empty" style="margin-top:22px">'
            + "<br>".join(
                escape(e)
                for e in notes[:8]
            )
            + "</p>"
        )

    html = f"""<html>
<head>
<meta charset="utf-8">
<style>{CSS}</style>
</head>

<body>
  <div class="wrap">

    <h1>Job Radar</h1>

    <p class="sub">
      {escape(period_label)}
      · {len(new_jobs)} new
      · {len(closed_jobs)} closed
    </p>

    <h2>Internships ({len(internships)})</h2>

    {block(
        internships,
        "No new matching internships today."
    )}

    <h2>Full-time ({len(full_time)})</h2>

    {block(
        full_time,
        "No new matching full-time roles today."
    )}

    <h2>Other / Review ({len(other)})</h2>

    {block(
        other,
        "Nothing to review."
    )}

    <h2>Closed / Filled</h2>

    {closed_html}

    {err_html}

    <p class="foot">
      Sent by JobRadar running on GitHub Actions.
      Application links point to the job posting URL
      collected by JobRadar.
      Scores come from an LLM reading your resume
      against each posting — treat them as a sort order,
      not a verdict.
    </p>

  </div>
</body>
</html>"""

    lines = [
        f"JOB RADAR — {period_label}",
        "",
        f"INTERNSHIPS ({len(internships)})",
    ]

    for r in internships:

        score = (
            f"[{r['score']}/10] "
            if r["score"] is not None
            else ""
        )

        lines.extend(
            [
                f"{score}{r['title']} — {r['company']}",
                f"Location: {r['location'] or 'n/a'}",
                f"Salary: {r['salary'] or 'Not listed'}",
                f"Apply: {r['url'] or 'n/a'}",
                "",
            ]
        )

    lines.append(
        f"FULL-TIME ({len(full_time)})"
    )

    for r in full_time:

        score = (
            f"[{r['score']}/10] "
            if r["score"] is not None
            else ""
        )

        lines.extend(
            [
                f"{score}{r['title']} — {r['company']}",
                f"Location: {r['location'] or 'n/a'}",
                f"Salary: {r['salary'] or 'Not listed'}",
                f"Apply: {r['url'] or 'n/a'}",
                "",
            ]
        )

    if other:
        lines.append(
            f"OTHER / REVIEW ({len(other)})"
        )

        for r in other:
            lines.extend(
                [
                    f"{r['title']} — {r['company']}",
                    f"Location: {r['location'] or 'n/a'}",
                    f"Apply: {r['url'] or 'n/a'}",
                    "",
                ]
            )

    for r in closed_jobs:
        lines.append(
            f"CLOSED: {r['title']} — {r['company']}"
        )

    return (
        html,
        "\n".join(lines) or "No updates."
    )


def send(
    cfg: dict,
    subject: str,
    html: str,
    text: str,
) -> None:

    password = (
        os.environ.get(
            "JOBRADAR_SMTP_PASSWORD"
        ) or ""
    ).replace(" ", "").strip()

    if not password:
        raise RuntimeError(
            "JOBRADAR_SMTP_PASSWORD is not set. "
            "Create a Gmail App Password and export it."
        )

    if password.startswith("paste-your"):
        raise RuntimeError(
            "JOBRADAR_SMTP_PASSWORD is still "
            "the placeholder in run.sh."
        )

    msg = EmailMessage()

    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]

    msg.set_content(text)
    msg.add_alternative(
        html,
        subtype="html",
    )

    try:

        with smtplib.SMTP(
            cfg["smtp_host"],
            cfg["smtp_port"],
            timeout=30,
        ) as s:

            s.starttls()

            s.login(
                cfg["from"],
                password,
            )

            s.send_message(msg)

    except (
        smtplib.SMTPAuthenticationError,
        smtplib.SMTPServerDisconnected,
    ) as e:

        raise RuntimeError(
            f"Gmail rejected the login for "
            f"{cfg['from']}. Check that:\n"
            f"  - the app password is the bare "
            f"16 characters\n"
            f"  - email.from in config.yaml is "
            f"the same account that generated it\n"
            f"  - 2-Step Verification is still "
            f"on for that account\n"
            f"(underlying: {type(e).__name__})"
        ) from e
