"""Same-day alerts for companies you care about.

The weekly digest is for browsing. This is for the handful of employers where
a week's delay actually costs you something: it runs often, and the moment a
matching role appears it mails you and raises a desktop notification.
"""
from __future__ import annotations

import subprocess
from html import escape

from . import digest as digest_mod


def notify_mac(title: str, message: str) -> None:
    """Desktop banner. Best effort - never let this break a run."""
    try:
        # osascript takes the strings as AppleScript literals, so quotes and
        # backslashes in a job title must be escaped or the script fails.
        def lit(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')[:200]

        subprocess.run(
            ["osascript", "-e",
             f'display notification "{lit(message)}" with title "{lit(title)}"'],
            check=False, capture_output=True, timeout=10,
        )
    except Exception:  # noqa: BLE001 - a missing binary must not fail the scan
        pass


def watched_names(cfg: dict) -> set[str]:
    """Empty list means every configured company is watched."""
    names = cfg.get("alerts", {}).get("companies") or []
    if names:
        return set(names)
    return {e["name"] for e in cfg.get("companies", [])}


def pick(rows, cfg: dict, baselined: set[str] | None = None) -> list:
    """New jobs at a watched company that clear the alert threshold.

    `baselined` is the set of companies already established. A company being
    seen for the first time has its whole board looking "new", which would fire
    dozens of alerts for jobs that have been open for months. Those are
    recorded silently instead, and alerts start from the next genuinely new
    posting.

    Passing None disables that check. It is NOT the same as passing an empty
    set, which would treat every company as unseen and silence every alert -
    the safer default is to alert, not to go quiet.
    """
    watch = watched_names(cfg)
    floor = int(cfg.get("alerts", {}).get("min_score",
                                          cfg.get("llm", {}).get("min_score", 6)))
    out = []
    for r in rows:
        if r["company"] not in watch:
            continue
        if baselined is not None and r["company"] not in baselined:
            continue                      # first sighting: baseline, do not alert
        # An unscored job is reported: better a look you did not need than a
        # missed opening because scoring had not caught up.
        if r["score"] is not None and r["score"] < floor:
            continue
        out.append(r)
    return out


def render(rows, label: str) -> tuple[str, str]:
    items = "".join(
        f'''<div class="job">
              {'<span class="score">%d/10</span>' % r["score"] if r["score"] is not None else ''}
              <a class="t" href="{escape(r["url"] or "#")}">{escape(r["title"])}</a>
              <div class="m">{escape(r["company"])} · {escape(r["location"] or "location n/a")}</div>
              {'<div class="why">%s</div>' % escape(r["reason"]) if r["reason"] else ''}
            </div>''' for r in rows)
    html = f"""<html><head><meta charset="utf-8"><style>{digest_mod.CSS}</style></head><body>
      <div class="wrap">
        <h1>Just opened</h1>
        <p class="sub">{escape(label)} · {len(rows)} new at companies you watch</p>
        {items}
        <p class="foot">Sent because these employers are in your alerts list.
        Change it under <code>alerts.companies</code> in config.yaml.</p>
      </div></body></html>"""
    text = "\n".join(
        f"{'[%d/10] ' % r['score'] if r['score'] is not None else ''}{r['title']} — "
        f"{r['company']} ({r['location'] or 'n/a'})\n  {r['url']}" for r in rows)
    return html, text


def send(cfg: dict, rows, label: str) -> None:
    html, text = render(rows, label)
    first = rows[0]
    subject = (f"Just opened: {first['title']} at {first['company']}"
               if len(rows) == 1 else
               f"Just opened: {len(rows)} roles at companies you watch")
    digest_mod.send(cfg["email"], subject, html, text)
    notify_mac("Job Radar", subject.replace("Just opened: ", ""))
